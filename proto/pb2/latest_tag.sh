#!/usr/bin/env bash

current_branch=$(git rev-parse --abbrev-ref HEAD)
if [ "$1" = "second_latest" ]; then
  latest_tag=$(git tag -l --sort=-creatordate --merged ${current_branch} | head -n 2 | tail -n 1)
else
  latest_tag=$(git tag -l --sort=-creatordate --merged ${current_branch} | head -n 1)
fi
echo ${latest_tag}
