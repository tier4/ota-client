#!/usr/bin/env bash
set -oe pipefail

SCRIPT=$(basename $0)

tag=${1:-main}
install_dir=${2:-/opt/ota/client}

echo "# install ota client: $tag to $install_dir ..."

tmp_dir=$(mktemp -dt ${SCRIPT}-XXX)
curl -s -H 'Accept: application/vnd.github.v3.raw' -L https://api.github.com/repos/tier4/ota-client/tarball/${tag} | tar xzf - -C ${tmp_dir}
src_dir=$(readlink -e ${tmp_dir}/tier4-ota-client-*)
if [ -z "$src_dir" ];then
    echo "# failed to download ota-client"
    exit 1
fi
echo "# install from $src_dir"

echo "# stop and disable otaclient.servcie"
systemctl stop otaclient.service || true
systemctl disable otaclient.service || true

echo "# remove old version"
rm -rf ${install_dir}/app
rm -f /etc/systemd/system/otaclient.service

echo "# install new version"
mkdir -p $install_dir
cp -a ${src_dir}/app ${install_dir}/
cp -a ${src_dir}/systemd/otaclient.service /etc/systemd/system/
python3 -m pip install -r ${install_dir}/app/requirements.txt

for file in ecuid ecuinfo.yaml;do
    mkdir -p /boot/ota
    if [ ! -f /boot/ota/${file} ];then
        cp -a ${src_dir}/boot/ota/${file} /boot/ota/
    fi
done

echo "# start and enable otaclient.servcie"
systemctl daemon-reload
systemctl start otaclient.service
systemctl enable otaclient.service

echo "# completed!!"
