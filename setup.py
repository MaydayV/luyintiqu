from setuptools import setup

APP = ['app.py']
OPTIONS = {
    'argv_emulation': False,
    'iconfile': None,
    'plist': {
        'CFBundleName': '录音提取工具',
        'CFBundleDisplayName': '录音提取工具',
        'CFBundleIdentifier': 'com.colin.luyintiqu',
        'CFBundleVersion': '1.0.0',
        'CFBundleShortVersionString': '1.0.0',
        'LSMinimumSystemVersion': '12.0',
    },
}

setup(
    app=APP,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)
