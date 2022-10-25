#!/usr/bin/env bash

current_branch=$(git rev-parse --abbrev-ref HEAD)
latest_tag=$(git tag -l --sort=-creatordate --merged ${current_branch} | head -n 1)
echo ${latest_tag}
