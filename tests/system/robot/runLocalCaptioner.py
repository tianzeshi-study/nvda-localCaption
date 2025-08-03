# A part of NonVisual Desktop Access (NVDA)
# Copyright (C) 2025 NV Access Limited, tianze
# This file may be used under the terms of the GNU General Public License, version 2 or later.
# For more details see: https://www.gnu.org/licenses/gpl-2.0.html

"""Logic for runLocalCaptioner tests."""

from datetime import datetime as _datetime
import tempfile
import os

from typing import Callable as _Callable
from robot.libraries.BuiltIn import BuiltIn

# relative import not used for 'systemTestUtils' because the folder is added to the path for 'libraries'
# imported methods start with underscore (_) so they don't get imported into robot files as keywords
from SystemTestSpy import (
	_getLib,
	_blockUntilConditionMet,
)
from SystemTestSpy.windows import getWindowHandle, waitUntilWindowFocused, windowWithHandleExists
from SystemTestSpy.mockModels import MockVisionEncoderDecoderGenerator

# Imported for type information
from robot.libraries.OperatingSystem import OperatingSystem as _OpSysLib

from AssertsLib import AssertsLib as _AssertsLib

import NvdaLib as _nvdaLib
from NvdaLib import NvdaLib as _nvdaRobotLib

_nvdaRobot: _nvdaRobotLib = _getLib("NvdaLib")
_opSys: _OpSysLib = _getLib("OperatingSystem")
_builtIn: BuiltIn = BuiltIn()
_asserts: _AssertsLib = _getLib("AssertsLib")


def update_local_model_path(output_dir):
	"""
	Update only the value of 'localModelPath' under [captionLocal] section
	in the INI file, preserving original formatting, indentation, and casing.
	"""
	# Normalize the path for Windows (e.g., use backslashes)
	new_path = os.path.normpath(output_dir)

	# Path to the INI file
	ini_path = os.path.join(
		os.path.dirname(__file__),
		"..", "nvdaSettingsFiles", "standard-doLoadMockModel.ini"
	)

	# Read original lines
	with open(ini_path, "r", encoding="utf-8") as f:
		lines = f.readlines()

	# Flags to track if we are in the [captionLocal] section
	in_caption_section = False

	# Updated lines will be stored here
	updated_lines = []

	for line in lines:
		# Detect section headers
		strip_line = line.strip()
		if strip_line.startswith("[") and strip_line.endswith("]"):
			in_caption_section = (strip_line.lower() == "[captionlocal]")

		# If inside captionLocal section, and line contains localModelPath (case-insensitive)
		if in_caption_section and "localModelPath" in line:
			# Preserve original indentation and formatting
			prefix, sep, _ = line.partition("=")
			updated_line = f"{prefix}{sep} {new_path}\n"
			updated_lines.append(updated_line)
		else:
			# Keep line as is
			updated_lines.append(line)

	# Write back the updated lines
	with open(ini_path, "w", encoding="utf-8") as f:
		f.writelines(updated_lines)
 
def _getNvdaMessageWindowhandle() -> int:
	return getWindowHandle(windowClassName="wxWindowClassNR", windowName="NVDA")


def _nvdaIsRunning() -> bool:
	return bool(_getNvdaMessageWindowhandle())


def NVDA_Caption():
	generator = MockVisionEncoderDecoderGenerator(random_seed=8)
	# Generate all files relative to repo root
	tempDir = tempfile.gettempdir()
	output_directory = os.path.join(tempDir, "nvdaProfile", "models", "mock", "vit-gpt2-image-captioning")
	print("models directory", os.path.realpath(output_directory));
	generator.generate_all_files(output_directory)
	# It seems that the location of the temp folder can notbe determined in the nvda.ini file
	update_local_model_path(output_directory)
	spy = _nvdaLib.getSpyLib()
	# open something to generate caption 
	spy.emulateKeyPress("NVDA+n")
	spy.emulateKeyPress("NVDA+windows+,")
	spy.wait_for_specific_speech("child person building building building building building building building building building")
	# spy.wait_for_specific_speech("NVDA free for free for free for free for free for free for free for free for free for free")



