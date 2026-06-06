from setuptools import setup
from setuptools.command.install import install as _install


class install(_install):
    def run(self):
        super().run()
        print("\n" + "=" * 60)
        print('Run "isonome pnp init" to enable plug-and-play.')
        print("=" * 60 + "\n")


setup(cmdclass={"install": install})
