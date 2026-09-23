# Avisos de terceros — UHD Blu-ray Toolkit

Este documento acompaña a la imagen `ghcr.io/aldicarus/hdo-iso-converter` y
al código de este repositorio. Enumera el software de terceros que la imagen
contiene, bajo qué licencia, y dónde obtener su código fuente.

## Cómo se relaciona la app con estas herramientas

La aplicación **invoca** `ffmpeg`, `ffprobe`, `mkvmerge`, `mkvpropedit`,
`mkvextract`, `mediainfo` y `dovi_tool` como **procesos independientes**, por
línea de comandos y tuberías (`subprocess`, `os.pipe()`). No enlaza contra
sus librerías ni incorpora su código.

Por eso el código de esta aplicación se distribuye bajo la licencia MIT (ver
`LICENSE`), mientras que las herramientas conservan la suya. Esa separación
no altera las obligaciones de **redistribución** de los binarios que la
imagen sí contiene, que son las que este documento atiende.

## Componentes distribuidos en la imagen

| Componente | Versión | Licencia | Fuente |
|---|---|---|---|
| **FFmpeg** / ffprobe (build estático de BtbN, `/usr/local/bin`) | `n7.1.5-12-g1fdbca85aa` | **GPL-3.0-or-later** (`--enable-gpl --enable-version3`) | [ffmpeg @ `1fdbca85aa`](https://github.com/FFmpeg/FFmpeg/commit/1fdbca85aa) · [BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds) · espejo abajo |
| **FFmpeg** (paquete de Ubuntu, respaldo en `/usr/bin`) | ver `app/static/licenses/INSTALLED-PACKAGES.txt` | **GPL-3.0-or-later** | `apt-get source ffmpeg` · [archive.ubuntu.com](http://archive.ubuntu.com/ubuntu/pool/universe/f/ffmpeg/) |
| **MKVToolNix** (`mkvmerge`, `mkvpropedit`, `mkvextract`) | ver `app/static/licenses/INSTALLED-PACKAGES.txt` | **GPL-2.0** | [mkvtoolnix.download/source.html](https://mkvtoolnix.download/source.html) · [codeberg.org/mbunkus/mkvtoolnix](https://codeberg.org/mbunkus/mkvtoolnix) |
| **MediaInfo** | ver `app/static/licenses/INSTALLED-PACKAGES.txt` | BSD-2-Clause | [mediaarea.net](https://mediaarea.net/en/MediaInfo/Download/Source) |
| **dovi_tool** | `2.3.3` | MIT | [github.com/quietvoid/dovi_tool](https://github.com/quietvoid/dovi_tool) |
| **Ubuntu** (imagen base) | `22.04` | mezcla: GPL-2.0, GPL-3.0, LGPL, BSD, MIT… | `apt-get source <paquete>` · [archive.ubuntu.com](http://archive.ubuntu.com/ubuntu/) |
| **Dependencias Python** | ver [`app/static/licenses/PYTHON-DEPENDENCIES.md`](app/static/licenses/PYTHON-DEPENDENCIES.md) | permisivas, más MPL-2.0 (`certifi`) | [PyPI](https://pypi.org/) |

`INSTALLED-PACKAGES.txt` lo escribe el propio build con
`dpkg-query`: es la lista exacta de paquetes y versiones que lleva **esa**
imagen. Los paquetes de apt no están fijados a una versión concreta, así que
es la única respuesta fiable a «¿de qué versión necesito la fuente?».

**No se distribuye** Sortable.js: se carga desde un CDN en tiempo de
ejecución (MIT, [SortableJS/Sortable](https://github.com/SortableJS/Sortable)).

## Avisos de copyright requeridos

Reproducidos literalmente, tal como exigen sus licencias:

> This product uses MediaInfo library, Copyright (c) 2002-2026 MediaArea.net SARL.

> dovi_tool — MIT License, Copyright (c) 2026 quietvoid

> This product uses the TMDB API but is not endorsed or certified by TMDB.

Los avisos de las dependencias Python están en
[`app/static/licenses/PYTHON-DEPENDENCIES.md`](app/static/licenses/PYTHON-DEPENDENCIES.md).

En la imagen en marcha, todo esto se sirve en `/static/licenses/`.

## Textos completos de las licencias

En el directorio [`app/static/licenses/`](app/static/licenses/): `GPL-2.0.txt`, `GPL-3.0.txt`,
`LGPL-3.0.txt` y `Apache-2.0.txt`. Las licencias permisivas (MIT, BSD-2,
BSD-3, ISC) se reproducen junto a cada componente en el apéndice de Python.

## Oferta escrita de código fuente / Written offer for source code

**English.** For a period of three (3) years from the date of distribution of
any binary version of this software, Aldicarus hereby offers to give any
third party, for a charge no more than the cost of physically performing
source distribution, a complete machine-readable copy of the corresponding
source code for the GPL- and LGPL-licensed components listed above, to be
delivered under the terms of their respective licenses. To request it, open
an issue at:

**https://github.com/Aldicarus/hdo-iso-converter/issues**

**Castellano.** Durante tres (3) años desde la fecha de distribución de
cualquier versión binaria de este software, Aldicarus ofrece entregar a
cualquier tercero, por un precio no superior al coste material de la
distribución, una copia completa y legible por máquina del código fuente
correspondiente a los componentes cubiertos por la GPL y la LGPL listados
arriba, bajo los términos de sus respectivas licencias. Para solicitarlo,
abre una incidencia en el enlace de arriba.

### Espejo de las fuentes

Los autobuilds de BtbN **se borran**: solo sobrevive el último de cada mes,
así que apuntar a su release como única fuente convertiría esta oferta en un
enlace roto. El binario de ffmpeg queda identificado por dos commits, los
dos permanentes:

| qué | commit |
|---|---|
| FFmpeg | [`1fdbca85aaea513c9cc6c14d347f76543346d3da`](https://github.com/FFmpeg/FFmpeg/commit/1fdbca85aaea513c9cc6c14d347f76543346d3da) (30-jul-2026) |
| BtbN/FFmpeg-Builds (los scripts que lo compilan, y que fijan la versión y la URL de cada dependencia) | [`a99e8230eae00d1cee38f23076a7a1f55cd984e2`](https://github.com/BtbN/FFmpeg-Builds/commit/a99e8230eae00d1cee38f23076a7a1f55cd984e2) (29-jul-2026) |

Los dos árboles están además archivados en las *releases* de este
repositorio, bajo el tag `sources-ffmpeg-n7.1.5`, por si alguno de los dos
repositorios de origen desapareciera.

Las de Ubuntu y MKVToolNix no se archivan: Canonical y mkvtoolnix.download
mantienen sus fuentes de forma duradera y los enlaces de la tabla apuntan
ahí.

## Dolby Vision

«Dolby», «Dolby Vision» y el símbolo de la doble D son marcas registradas de
Dolby Laboratories Licensing Corporation. Esta aplicación no está afiliada a
Dolby Laboratories ni respaldada por ellos, y no incluye ninguna
implementación bajo licencia de Dolby: se limita a leer y reescribir metadata
ya presente en los ficheros del usuario, mediante `dovi_tool`.
