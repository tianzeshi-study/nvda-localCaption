# A part of NonVisual Desktop Access (NVDA)
# Copyright (C) 2022-2025 NV Access Limited
# This file is covered by the GNU General Public License.
# See the file COPYING for more details.

from concurrent.futures import (
	Future,
	ThreadPoolExecutor,
)
import hashlib
import os
import pathlib
import shutil
from typing import (
	TYPE_CHECKING,
	cast,
	Callable,
	Dict,
	Optional,
	Tuple,
)
from urllib.parse import urlparse

import requests

import addonAPIVersion
from core import callLater
from logHandler import log
import NVDAState
from NVDAState import WritePaths
import threading
from utils.security import sha256_checksum
from utils.networking import (
	_getCertificate,
	_is_cert_verification_error,
	_updateWindowsRootCertificates,
)
from config import conf

from .models.addon import (
	_AddonGUIModel,
	_AddonStoreModel,
)
from .models.channel import Channel


if TYPE_CHECKING:
	from gui.message import DisplayableError
	from gui.addonStoreGui.viewModels.addonList import AddonListItemVM


_DEFAULT_BASE_URL = "https://addonStore.nvaccess.org"
_LATEST_API_VER = "latest"
"""
A string value used in the add-on store to fetch the latest version of all add-ons,
i.e include older incompatible versions.
"""


def _getBaseURL() -> str:
	if url := conf["addonStore"]["baseServerURL"]:
		return url
	return _DEFAULT_BASE_URL


def _getCurrentApiVersionForURL() -> str:
	year, major, minor = addonAPIVersion.CURRENT
	return f"{year}.{major}.{minor}"


def _getAddonStoreURL(channel: Channel, lang: str, nvdaApiVersion: str) -> str:
	return f"{_getBaseURL()}/{lang}/{channel.value}/{nvdaApiVersion}.json"


def _getCacheHashURL() -> str:
	return f"{_getBaseURL()}/cacheHash.json"


class AddonFileDownloader:
	OnCompleteT = Callable[
		["AddonListItemVM[_AddonStoreModel]", Optional[os.PathLike]],
		None,
	]

	DOWNLOAD_LOCK = threading.RLock()
	"""Used to protect cross-thread download management.

	Notably:
	- tracking download progress: AddonFileDownloader.progress
	- writes/reads to an add-on file: _AddonStoreModel.tempDownloadPath
	"""

	def __init__(self):
		self.progress: Dict["AddonListItemVM[_AddonStoreModel]", int] = {}
		"""
		Counts chunks received in a download of an add-on.

		Usage should be protected by AddonFileDownloader.DOWNLOAD_LOCK.
		"""

		self._pending: Dict[
			Future[Optional[os.PathLike]],
			Tuple[
				"AddonListItemVM[_AddonStoreModel]",
				AddonFileDownloader.OnCompleteT,
				"DisplayableError.OnDisplayableErrorT",
			],
		] = {}
		self.complete: Dict[
			"AddonListItemVM[_AddonStoreModel]",
			# Path to downloaded file
			Optional[os.PathLike],
		] = {}
		self._executor = ThreadPoolExecutor(
			max_workers=10,
			thread_name_prefix="AddonDownloader",
		)

		if NVDAState.shouldWriteToDisk():
			# empty temporary downloads
			if os.path.exists(WritePaths.addonStoreDownloadDir):
				shutil.rmtree(WritePaths.addonStoreDownloadDir)
			# ensure downloads dir exist
			pathlib.Path(WritePaths.addonStoreDownloadDir).mkdir(parents=True, exist_ok=True)

	def download(
		self,
		addonData: "AddonListItemVM[_AddonStoreModel]",
		onComplete: OnCompleteT,
		onDisplayableError: "DisplayableError.OnDisplayableErrorT",
	):
		# Initialize progress for this download.
		# This is done before submitting the download task to the executor,
		# to ensure that the download can be cancelled before it starts.
		# No lock is needed here, as the download task will not have started yet.
		self.progress[addonData] = 0
		assert self._executor
		f: Future[Optional[os.PathLike]] = self._executor.submit(
			self._download,
			addonData,
		)
		self._pending[f] = addonData, onComplete, onDisplayableError
		f.add_done_callback(self._done)

	def _done(self, downloadAddonFuture: Future[Optional[os.PathLike]]):
		with self.DOWNLOAD_LOCK:
			isCancelled = (
				downloadAddonFuture.cancelled()
				or downloadAddonFuture not in self._pending
				or self._pending[downloadAddonFuture][0] not in self.progress
			)
		addonId = "CANCELLED" if isCancelled else self._pending[downloadAddonFuture][0].model.addonId
		log.debug(f"Done called for {addonId}")

		if not downloadAddonFuture.done():
			log.error("Logic error with download in BG thread.")
			isCancelled = True

		if isCancelled:
			log.debug("Download was cancelled, not calling onComplete")
			try:
				with self.DOWNLOAD_LOCK:
					# If the download was cancelled, the file may have been partially downloaded.
					os.remove(self._pending[downloadAddonFuture][0].model.tempDownloadPath)
			except FileNotFoundError:
				pass
			except Exception as e:
				log.error(f"Error while deleting partially downloaded file: {e}")
			return

		addonData, onComplete, onDisplayableError = self._pending[downloadAddonFuture]
		downloadAddonFutureException = downloadAddonFuture.exception()
		cacheFilePath: Optional[os.PathLike]
		if downloadAddonFutureException:
			cacheFilePath = None
			from gui.message import DisplayableError

			if not isinstance(downloadAddonFutureException, DisplayableError):
				log.error("Unhandled exception in _download", exc_info=downloadAddonFuture.exception())
			else:
				callLater(
					delay=0,
					callable=onDisplayableError.notify,
					displayableError=downloadAddonFutureException,
				)
		else:
			cacheFilePath = downloadAddonFuture.result()

		# If canceled after our previous isCancelled check,
		# then _pending and progress will be empty.
		with self.DOWNLOAD_LOCK:
			self._pending.pop(downloadAddonFuture, None)
			self.progress.pop(addonData, None)
			self.complete[addonData] = cacheFilePath
		onComplete(addonData, cacheFilePath)

	def cancelAll(self):
		log.debug("Cancelling all")
		futuresCopy = self._pending.copy()
		for f in futuresCopy.keys():
			f.cancel()
		assert self._executor
		self._executor.shutdown(wait=False)
		self._executor = None
		with self.DOWNLOAD_LOCK:
			self.progress.clear()
			self._pending.clear()
		shutil.rmtree(WritePaths.addonStoreDownloadDir)

	def _downloadAddonToPath(
		self,
		addonData: "AddonListItemVM[_AddonStoreModel]",
		downloadFilePath: str,
	) -> bool:
		"""
		@return: True if the add-on is downloaded successfully,
		False if the download is cancelled
		"""
		if not NVDAState.shouldWriteToDisk():
			log.error("Should not write to disk, cancelling download")
			return False

		# Some add-ons are quite large, so we need to allow for a long download time.
		# 1GB at 0.5 MB/s takes 4.5hr to download.
		MAX_ADDON_DOWNLOAD_TIME = 60 * 60 * 6  # 6 hours
		with requests.get(
			addonData.model.URL,
			stream=True,
			timeout=MAX_ADDON_DOWNLOAD_TIME,
		) as r:
			with open(downloadFilePath, "wb") as fd:
				# Most add-ons are small. This value was chosen quite arbitrarily, but with the intention to allow
				# interrupting the download. This is particularly important on a slow connection, to provide
				# a responsive UI when cancelling.
				# A size has been selected attempting to balance the maximum throughput, with responsiveness for
				# users with a slow connection.
				# This could be improved by dynamically adjusting the chunk size based on the time elapsed between
				# chunk, starting with small chunks and increasing up until a maximum wait time is reached.
				chunkSize = 128000
				for chunk in r.iter_content(chunk_size=chunkSize):
					with self.DOWNLOAD_LOCK:
						fd.write(chunk)
						if addonData in self.progress:  # Removed when the download should be cancelled.
							self.progress[addonData] += 1
						else:
							log.debug(f"Cancelled download: {addonData.model.addonId}")
							return False  # The download was cancelled
		return True

	# Translators: A title for a dialog notifying a user of an add-on download failure.
	_ADDON_DOWNLOAD_FAILURE_MESSAGE_TITLE = pgettext("addonStore", "Add-on download failure")

	def _handleCertVerificationError(
		self,
		exception: requests.exceptions.SSLError,
		listItem: "AddonListItemVM[_AddonStoreModel]",
	) -> os.PathLike | None:
		from gui.message import DisplayableError, MessageDialog, ReturnCode

		if _is_cert_verification_error(exception):
			cert = _getCertificate(listItem.model.URL)
			certFingerprint = hashlib.sha256(cert).hexdigest()

			if (
				MessageDialog.confirm(
					message=pgettext(
						"addonStore",
						# Translators: A message to the user if an add-on download fails.
						# url is replaced with the base URL of the add-on download e.g. (github.com).
						# fingerprint is replaced with the SHA256 fingerprint of the certificate.
						"The website where you are downloading the add-on from has a certificate that is not trusted. "
						"Do you want to trust the root certificate for {url}? "
						"This will allow you to download add-ons from this website in the future. "
						"Only do this if you trust the website. "
						"The certificate's SHA256 fingerprint is: {fingerprint}. ",
					).format(url=urlparse(listItem.model.URL).netloc, fingerprint=certFingerprint),
					caption=self._ADDON_DOWNLOAD_FAILURE_MESSAGE_TITLE,
				)
				== ReturnCode.OK
			):
				_updateWindowsRootCertificates(cert)
				return self._download(listItem)
			else:
				return None  # The download was cancelled
		else:
			log.debugWarning(f"Unable to download addon file: {exception}")
			raise DisplayableError(
				pgettext(
					"addonStore",
					# Translators: A message to the user if an add-on download fails
					"Unable to download add-on: {name}",
				).format(name=listItem.model.displayName),
				self._ADDON_DOWNLOAD_FAILURE_MESSAGE_TITLE,
			)

	def _download(self, listItem: "AddonListItemVM[_AddonStoreModel]") -> os.PathLike | None:
		from gui.message import DisplayableError

		addonData = listItem.model
		log.debug(f"starting download: {addonData.addonId}")
		cacheFilePath = addonData.cachedDownloadPath
		if os.path.exists(cacheFilePath):
			log.debug(f"Cache file already exists, deleting {cacheFilePath}")
			os.remove(cacheFilePath)

		inProgressFilePath = addonData.tempDownloadPath
		with self.DOWNLOAD_LOCK:
			if listItem not in self.progress:
				log.debug("the download was cancelled before it started.")
				return None  # The download was cancelled
		try:
			if not self._downloadAddonToPath(listItem, inProgressFilePath):
				return None  # The download was cancelled
		except requests.exceptions.SSLError as e:
			return self._handleCertVerificationError(e, listItem)
		except requests.exceptions.RequestException as e:
			log.debugWarning(f"Unable to download addon file: {e}")
			raise DisplayableError(
				pgettext(
					"addonStore",
					# Translators: A message to the user if an add-on download fails
					"Unable to download add-on: {name}",
				).format(name=addonData.displayName),
				self._ADDON_DOWNLOAD_FAILURE_MESSAGE_TITLE,
			)
		except OSError as e:
			log.debugWarning(f"Unable to save addon file ({inProgressFilePath}): {e}")
			raise DisplayableError(
				pgettext(
					"addonStore",
					# Translators: A message to the user if an add-on download fails
					"Unable to save add-on as a file: {name}",
				).format(name=addonData.displayName),
				self._ADDON_DOWNLOAD_FAILURE_MESSAGE_TITLE,
			)
		if not self._checkChecksum(inProgressFilePath, addonData):
			with self.DOWNLOAD_LOCK:
				os.remove(inProgressFilePath)
			log.debugWarning(f"Cache file deleted, checksum mismatch: {inProgressFilePath}")
			raise DisplayableError(
				pgettext(
					"addonStore",
					# Translators: A message to the user if an add-on download is not safe
					"Add-on download not safe: checksum failed for {name}",
				).format(name=addonData.displayName),
				self._ADDON_DOWNLOAD_FAILURE_MESSAGE_TITLE,
			)
		log.debug(f"Download complete: {inProgressFilePath}")
		with self.DOWNLOAD_LOCK:
			os.replace(src=inProgressFilePath, dst=cacheFilePath)
		log.debug(f"Cache file available: {cacheFilePath}")
		return cast(os.PathLike, cacheFilePath)

	@staticmethod
	def _checkChecksum(addonFilePath: str, addonData: _AddonStoreModel) -> bool:
		with AddonFileDownloader.DOWNLOAD_LOCK:
			with open(addonFilePath, "rb") as f:
				sha256Addon = sha256_checksum(f)
		return sha256Addon.casefold() == addonData.sha256.casefold()

	@staticmethod
	def _getCacheFilenameForAddon(addonData: _AddonGUIModel) -> str:
		return f"{addonData.addonId}-{addonData.addonVersionName}.nvda-addon"

	def __del__(self):
		if self._executor is not None:
			self._executor.shutdown(wait=False)
			self._executor = None
