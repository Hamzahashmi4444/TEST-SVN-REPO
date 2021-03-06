from setuptools import setup, find_packages

setup(
    name='NaayaBundles-CHMBE',
    description = "Server-specific bundles, Belgian CHM",
    packages=find_packages(),
    include_package_data=True,
    zip_safe=False,
    install_requires=['naaya.chm', 'Products.CHM2BE'],
)
