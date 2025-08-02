# A part of NonVisual Desktop Access (NVDA)
# Copyright (C) 2025 NV Access Limited, tianze
# This file may be used under the terms of the GNU General Public License, version 2 or later.
# For more details see: https://www.gnu.org/licenses/gpl-2.0.html

"""Logic for runLocalCaptioner tests."""

from datetime import datetime as _datetime
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


def _getNvdaMessageWindowhandle() -> int:
	return getWindowHandle(windowClassName="wxWindowClassNR", windowName="NVDA")


def _nvdaIsRunning() -> bool:
	return bool(_getNvdaMessageWindowhandle())


def NVDA_Caption():
	generator = MockVisionEncoderDecoderGenerator(random_seed=8)
	# Generate all files relative to repo root
	output_directory = "./models/mock/vit-gpt2-image-captioning"
	generator.generate_all_files(output_directory)
	spy = _nvdaLib.getSpyLib()
	# open something to generate caption 
	spy.emulateKeyPress("NVDA+n")
	spy.emulateKeyPress("NVDA+windows+,")
	spy.wait_for_specific_speech("child person building building building building building building building building building")
	# spy.wait_for_specific_speech("NVDA free for free for free for free for free for free for free for free for free for free")



