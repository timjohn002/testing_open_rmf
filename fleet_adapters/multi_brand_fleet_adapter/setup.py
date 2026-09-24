from glob import glob

from setuptools import setup

package_name = 'multi_brand_fleet_adapter'

setup(
    name=package_name,
    version='0.1.0',
    packages=[package_name, f'{package_name}.drivers'],
    data_files=[
        ('share/ament_index/resource_index/packages',
            ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
        ('share/' + package_name + '/config', glob('config/*.yaml')),
        ('share/' + package_name + '/launch', glob('launch/*.launch.xml')),
    ],
    install_requires=['setuptools', 'requests', 'pyyaml', 'nudged'],
    zip_safe=True,
    maintainer='timjohn002',
    maintainer_email='timjohnargota@gmail.com',
    description='Multi-brand Open-RMF fleet adapter with pluggable drivers',
    license='Apache License 2.0',
    tests_require=['pytest'],
    entry_points={
        'console_scripts': [
            'fleet_adapter=multi_brand_fleet_adapter.fleet_adapter:main',
        ],
    },
)
