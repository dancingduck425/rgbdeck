# Maintainer: dancingduck425
# Build & install from the checkout:  cd ~/src/rgbdeck && makepkg -si
pkgname=rgbdeck
pkgver=1.3.2
pkgrel=1
pkgdesc="Standalone RGB lighting control for keyboards, mice and motherboards (own drivers, direct hidraw)"
arch=('any')
url="https://github.com/dancingduck425/rgbdeck"
license=('GPL-2.0-only')
depends=('python' 'pyside6' 'hicolor-icon-theme')
optdepends=('python-evdev: extra key names for typing effects on exotic keys')
source=()

package() {
  cd "$startdir"
  local site
  site="$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
  install -d "$pkgdir$site"
  cp -r rgbdeck "$pkgdir$site/"
  find "$pkgdir$site/rgbdeck" -name __pycache__ -type d -exec rm -rf {} +
  python -m compileall -q "$pkgdir$site/rgbdeck"
  install -Dm755 /dev/stdin "$pkgdir/usr/bin/rgbdeck" <<'SH'
#!/bin/sh
exec python -m rgbdeck "$@"
SH
  install -Dm644 rgbdeck/data/rgbdeck.desktop "$pkgdir/usr/share/applications/rgbdeck.desktop"
  install -Dm644 rgbdeck/data/rgbdeck.svg "$pkgdir/usr/share/icons/hicolor/scalable/apps/rgbdeck.svg"
  install -Dm644 rgbdeck/data/70-rgbdeck.rules "$pkgdir/usr/lib/udev/rules.d/70-rgbdeck.rules"
  install -Dm644 README.md "$pkgdir/usr/share/doc/rgbdeck/README.md"
}
