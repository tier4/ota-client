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
"""Greengrass config v1/v2 parsing implementation."""


from __future__ import annotations
from functools import partial
from pydantic import BaseModel, ConfigDict
from typing import Any, NamedTuple

from otaclient._utils import chain_query


class _FixedConfig(BaseModel):
    model_config = ConfigDict(frozen=True)


def remove_prefix(_str: str, _prefix: str) -> str:
    # NOTE: in py3.8 we don't have str.removeprefix yet.
    if _str.startswith(_prefix):
        return _str.replace(_prefix, "", 1)
    return _str


regulate_path = partial(remove_prefix, _prefix="file://")


class _ThingArn(NamedTuple):
    """
    Format:
        arn:partition:service:region:account-id:resource-id
        arn:partition:service:region:account-id:resource-type/resource-id
        arn:partition:service:region:account-id:resource-type:resource-id

    Check https://docs.aws.amazon.com/IAM/latest/UserGuide/reference-arns.html for details.
    """

    arn: str
    partition: str
    service: str
    region: str
    account_id: str
    resource_id: str


def parse_v1_config(_cfg: dict[str, Any]) -> GreengrassConfig:
    """Parse Greengrass V1 config json and take what we need.

    Check https://docs.aws.amazon.com/greengrass/v1/developerguide/gg-core.html
        for example of full version of config.json.
    """
    _raw_thing_arn: str = chain_query(_cfg, "coreThing:thingArn")
    _thing_arn = _ThingArn(*_raw_thing_arn.split(":", 6))
    return GreengrassConfig(
        ca_path=regulate_path(chain_query(_cfg, "crypto:caPath")),
        private_key_path=regulate_path(
            chain_query(_cfg, "crypto:principals:IoTCertificate:privateKeyPath")
        ),
        certificate_path=regulate_path(
            chain_query(_cfg, "crypto:principals:IoTCertificate:certificatePath")
        ),
        thing_name=remove_prefix(_thing_arn.resource_id, "thing/"),
        region=_thing_arn.region,
    )


def parse_v2_config(_cfg: dict[str, Any]) -> GreengrassConfig:
    """Parse Greengrass V2 config yaml and take what we need.

    Check https://github.com/aws4embeddedlinux/meta-aws/blob/master/recipes-iot/aws-iot-greengrass/files/greengrassv2-init.yaml
        for example of full version of ggv2 config.yaml.

    For TPM2.0, see https://docs.aws.amazon.com/greengrass/v2/developerguide/hardware-security.html.
    """

    return GreengrassConfig(
        ca_path=chain_query(_cfg, "system:rootCaPath"),
        private_key_path=chain_query(_cfg, "system:privateKeyPath"),
        certificate_path=chain_query(_cfg, "system:certificateFilePath"),
        thing_name=chain_query(_cfg, "system:thingName"),
        region=chain_query(
            _cfg, "services:aws.greengrass.Nucleus:configuration:awsRegion"
        ),
    )


class GreengrassConfig(_FixedConfig):
    """Configurations we need picked from parsed Greengrass V1/V2 configration file."""

    ca_path: str
    private_key_path: str
    certificate_path: str
    thing_name: str
    region: str
