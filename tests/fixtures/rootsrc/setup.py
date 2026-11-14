from setuptools import setup

ENTRY = {"console_scripts": ["demo = demo.cli:main"], "demo.plugins": ["alpha = demo.plug:Alpha"]}

setup(
    name="demo",
    entry_points=ENTRY,
    scripts=["scripts/run.sh"],
)
