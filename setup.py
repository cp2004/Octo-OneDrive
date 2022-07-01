import setuptools

import versioneer

NAME = "octo_onedrive"
VERSION = versioneer.get_version()
DEPENDENCIES = ["msal>=1.18.0,<2", "cryptography"]

with open("README.md", encoding="utf-8") as fh:
    long_description = fh.read()

setuptools.setup(
    name=NAME,
    version=VERSION,
    author="Charlie Powell",
    author_email="cp2004.github@gmail.com",
    description="OctoPrint OneDrive Communication Module",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/cp2004/Octo-OneDrive",
    project_urls={
        "Bug Tracker": "https://github.com/cp2004/Octo-OneDrive/issues",
    },
    classifiers=[
        "Programming Language :: Python :: 3 :: Only",
        "License :: OSI Approved :: GNU Affero General Public License v3",
        "Operating System :: OS Independent",
        "Development Status :: 5 - Production/Stable",
    ],
    package_dir={"": "src"},
    packages=setuptools.find_packages(where="src"),
    python_requires=">=3.7",
    cmdclass=versioneer.get_cmdclass(),
    install_requires=DEPENDENCIES,
    extras_require={"develop": ["pre-commit"]},
)
