import setuptools

with open("README.md", "r") as fh:
    long_description = fh.read()

setuptools.setup(
    name="sentineleof",
    version="0.7.0",
    author="Scott Staniewicz",
    author_email="scott.stanie@gmail.com",
    description="Download precise orbit files for Sentinel 1 products",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/scottstanie/sentineleof",
    packages=setuptools.find_packages(),
    include_package_data=True,
    classifiers=(
        "Programming Language :: Python",
        "Programming Language :: Python :: 3.5",
        "Programming Language :: Python :: 3.6",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "License :: OSI Approved :: MIT License",
        "Topic :: Scientific/Engineering",
        "Intended Audience :: Science/Research",
    ),
    install_requires=[
        "requests",
        "click",
        "python-dateutil",
        "sentinelsat >= 1.0",
    ],
    entry_points={
        "console_scripts": [
            "eof=eof.cli:cli",
        ],
    },
    zip_safe=False,
)
