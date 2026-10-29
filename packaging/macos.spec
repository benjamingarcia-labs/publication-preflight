"""Native macOS bundle with release metadata set before ad-hoc signing."""
from pathlib import Path

root = Path(SPECPATH).parent
a = Analysis(
    [str(root / "launcher.py")],
    pathex=[str(root)],
    binaries=[],
    datas=[(str(root / "preflight" / "static"), "preflight/static")],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True,
          name="PublicationPreflight", debug=False, strip=False,
          upx=True, console=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False,
               upx=True, name="PublicationPreflight")
app = BUNDLE(coll, name="PublicationPreflight.app",
             bundle_identifier="com.benjaminjgarcia.publicationpreflight",
             version="1.0.0", info_plist={"CFBundleVersion": "1.0.0"})
