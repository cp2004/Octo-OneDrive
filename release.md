# Release Process

Quite simple 🙂

## Before release

1. Test it works

## Release

1. Push to GitHub, then create a release. If the tests/pre-commit pass then it will be built & uploaded to PyPi.

## After release

1. Bump version in OctoPrint-OneDrive-Backup plugin & OctoPrint-OneDriveFilesSync plugins - they are pinned to prevent the need for simultaneous releases for breaking changes & making testing easier.
