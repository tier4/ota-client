# Copyright 2022 TIER IV, INC. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from multiprocessing import freeze_support

if __name__ == "__main__":
    freeze_support()

    import sys

    # NOTE: otaclient takes no options; the first arg, when there is one, names a
    #   subcommand (version, mount-overlay), so no argparser is needed.
    if len(sys.argv) > 1 and sys.argv[1] == "version":
        from _otaclient_version import version

        print(version)
    elif len(sys.argv) > 1 and sys.argv[1] == "mount-overlay":
        # at boot, before local-fs.target: lay the applied overlay, see
        #   samples/otaclient-overlay.service_app; otaclient itself is not
        #   started (nor imported)
        from otaclient.overlay import mount_overlay_command

        sys.exit(mount_overlay_command())
    else:
        from otaclient import main

        main.main()
