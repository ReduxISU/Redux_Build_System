#!/usr/bin/env bash
set -e

source dev-container-features-test-lib

check "rbs on PATH" bash -c "command -v rbs"
check "rbs reports a version" bash -c "rbs --version"
check "operations are registered" bash -c "rbs --help | grep -q integration-test"
check "runs as the non-root user" bash -c "[ \"\$(id -u)\" -ne 0 ] && rbs --version"

reportResults
