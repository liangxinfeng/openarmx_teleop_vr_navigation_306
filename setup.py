from glob import glob
from setuptools import find_packages, setup


package_name = 'openarmx_teleop_vr_navigation_306'

setup(
    name=package_name,
    version='0.1.0',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages', ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml', 'README.md']),
        ('share/' + package_name + '/launch', glob('launch/*.launch.py')),
        ('share/' + package_name + '/web', glob('web/*.*')),
        ('share/' + package_name + '/config', glob('config/*.*')),
    ],
    install_requires=['setuptools'],
    scripts=[
        'scripts/start_vr_navigation_306.sh',
        'scripts/vr_navigation_desktop_app.py',
    ],
    zip_safe=False,
    maintainer='Autolife Robotics',
    maintainer_email='ubuntu@example.com',
    description='WebXR arm teleoperation with map, waypoint and base navigation for robot 306.',
    license='Proprietary',
    entry_points={
        'console_scripts': [
            'navigation_web_bridge = openarmx_teleop_vr_navigation_306.navigation_web_bridge:main',
            'corridor_projection_node = openarmx_teleop_vr_navigation_306.corridor_projection_node:main',
            'task_voice_web = openarmx_teleop_vr_navigation_306.task_voice_web:main',
            'task_speech_node = openarmx_teleop_vr_navigation_306.task_speech_node:main',
            'voice_mode_router = openarmx_teleop_vr_navigation_306.voice_mode_router:main',
        ],
    },
)
