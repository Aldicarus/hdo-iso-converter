<div align="center">

# UHD Blu-ray Toolkit

**Tus UHD Blu-ray, convertidos en MKV listos para Plex o Jellyfin, analizados a fondo y con su Dolby Vision actualizado a CMv4.0.**

Una aplicación web que se instala en el NAS y se usa desde el navegador.<br>
Sin recodificar: la imagen y el sonido son los del disco, bit a bit.

[![Última versión](https://img.shields.io/github/v/release/Aldicarus/hdo-iso-converter?sort=semver&filter=v*&label=versi%C3%B3n)](https://github.com/Aldicarus/hdo-iso-converter/releases/latest)
[![Docker](https://img.shields.io/badge/docker-ghcr.io-2496ED?logo=docker&logoColor=white)](https://github.com/Aldicarus/hdo-iso-converter/pkgs/container/hdo-iso-converter)
[![Licencia MIT](https://img.shields.io/badge/licencia-MIT-green)](LICENSE)

[Qué hace](#qué-hace) · [Instalación](#instalación) · [Funcionalidades](#funcionalidades) · [Documentación técnica](docs/DOCUMENTACION_TECNICA.md) · [Agradecimientos](#agradecimientos)

<img src="docs/img/cmv4-proyecto.png" alt="UHD Blu-ray Toolkit: un proyecto de upgrade Dolby Vision CMv4.0 con la ficha de la película, la recomendación de la comunidad y la columna de trabajos">

</div>

---

## Qué hace

Un UHD Blu-ray trae mucho más de lo que se ve: el audio en varios idiomas y formatos, decenas de subtítulos, capítulos y, casi siempre, **Dolby Vision**, la tecnología que ajusta el brillo y el color escena a escena según el televisor. Pasarlo a un fichero cómodo de reproducir sin perder nada de eso exige una docena de herramientas de línea de comandos y bastante paciencia.

**UHD Blu-ray Toolkit lo reúne todo en una sola aplicación**, con tres herramientas:

| | |
|---|---|
| 💿 **Blu-ray ISO → MKV** | Convierte el disco en un MKV con el mejor audio, los subtítulos que hacen falta y los capítulos. Películas y series, con los nombres que Plex y Jellyfin reconocen. |
| ✨ **Upgrade Dolby Vision CMv4.0** | Añade a tu MKV la versión moderna de los metadatos Dolby Vision, la que usan las plataformas de streaming: más precisa y con los ajustes del colorista para cada tipo de pantalla. |
| ✏️ **Consultar / Editar MKV** | Radiografía completa de cualquier MKV —audio, HDR, Dolby Vision y el brillo de la película de principio a fin— y edición instantánea de pistas y capítulos. |

Todo funciona en el NAS: los trabajos largos siguen adelante aunque se cierre el navegador, y la app avisa cuando terminan.

---

## Instalación

**Qué hace falta:** un NAS (como un QNAP con Container Station) o un ordenador con Linux y **Docker**, con procesador Intel o AMD de 64 bits. La aplicación se distribuye ya construida: no hay que compilar nada.

**1. Descargar los dos ficheros de configuración** en una carpeta nueva:

```bash
mkdir uhd-bluray-toolkit && cd uhd-bluray-toolkit
curl -LO https://raw.githubusercontent.com/Aldicarus/hdo-iso-converter/main/docker/docker-compose.yml
curl -L  https://raw.githubusercontent.com/Aldicarus/hdo-iso-converter/main/docker/.env.example -o .env
```

**2. Indicar las carpetas** editando el fichero `.env`. Cada una tiene que existir antes de arrancar:

| En `.env` | Qué carpeta |
|---|---|
| `ISOS_PATH` | Donde están los discos: ISOs, carpetas BDMV o ficheros M2TS |
| `OUTPUT_PATH` | Donde se guardan los MKV terminados |
| `LIBRARY_PATH` | Tu biblioteca de MKV, para consultarlos o actualizarlos *(opcional)* |
| `TMP_PATH` | Espacio de trabajo temporal; mejor en un SSD y con el doble del tamaño de una película |
| `CONFIG_PATH` | Donde la app guarda sus proyectos y ajustes |
| `CMV40_RPU_PATH` | Una carpeta vacía creada a propósito, si no tienes ficheros RPU propios |

**3. Arrancar:**

```bash
docker compose pull
docker compose up -d
```

**4. Abrir** `http://IP-DEL-NAS:8090` en el navegador. Listo.

<details>
<summary><b>Con Container Station (QNAP) o Portainer, sin línea de comandos</b></summary>

1. Descarga `docker-compose.yml` y `.env.example` desde los enlaces de arriba, renombra el segundo a `.env` y pon tus carpetas.
2. Crea una aplicación de tipo *Docker Compose* (Container Station: *Aplicación → Crear* · Portainer: *Stacks → Add stack*).
3. Pega el contenido de `docker-compose.yml`, añade el `.env` y despliega.

</details>

**Actualizar** es un solo comando, y la propia app avisa en la cabecera cuando hay una versión nueva:

```bash
docker compose pull && docker compose up -d
```

> [!TIP]
> La ficha de las películas y series (carátula, sinopsis, episodios) funciona nada más instalar. Para buscar ficheros en el repositorio DoviTools hace falta además una clave gratuita de Google, que se pega en ⚙︎ Configuración. El manual integrado explica paso a paso cómo conseguirla.

---

## Funcionalidades

### 💿 Del disco al MKV

<img src="docs/img/iso-a-mkv.png" alt="Selección de pistas de audio y subtítulos de un proyecto ISO → MKV">

- **Las pistas, elegidas solas.** El mejor audio en tu idioma y en versión original (TrueHD Atmos, DTS-HD MA…), los subtítulos forzados y completos que hacen falta y los capítulos del disco. Lo descartado se queda a la vista, con el motivo, y se recupera con un clic.
- **ISO, carpeta BDMV o ficheros M2TS.** El ISO se lee directamente, sin copiarlo ni descomprimirlo.
- **Series de televisión.** Reconoce los episodios del disco, los empareja con la temporada en TMDb y crea un MKV por episodio, con nombres del tipo `Serie (2011) - S03E01 - Título.mkv`.
- **Dolby Vision intacto**, incluida la capa de mejora FEL, y una comprobación final de que el MKV contiene exactamente lo que se pidió.
- **Una cola de trabajos** que procesa los discos uno tras otro, con progreso real y tiempo restante.

### ✨ Upgrade Dolby Vision CMv4.0

<p align="center"><img src="docs/img/cmv4-nuevo-proyecto.png" width="720" alt="Nuevo proyecto CMv4.0: la recomendación de la hoja DoviTools y los ficheros candidatos del repositorio"></p>

Dolby Vision viaja con unas instrucciones para que cada televisor adapte el brillo y el color a lo que es capaz de mostrar. Los Blu-ray traen la versión 2.9 de esas instrucciones; el streaming ya usa la **4.0**, con ajustes de colorista para cada tipo de pantalla. Esta herramienta lleva la 4.0 a tu MKV **sin tocar la imagen**.

- **Sabe qué películas lo admiten.** Consulta en vivo la hoja de la comunidad DoviTools y busca el fichero adecuado en su repositorio.
- **Dice si merece la pena.** Mide lo que aporta ese fichero frente a lo que ya tiene tu disco y recomienda actualizar o dejar el MKV como está.
- **Sincronización comprobada.** Si el fichero no cuadra fotograma a fotograma con tu edición, lo enseña en un gráfico y permite corregirlo antes de continuar.
- **Automático de principio a fin.** Elige el camino más rápido para cada caso, ejecuta todas las fases solo y etiqueta el resultado con su calidad (`[CMv4 FULL]`, `CORE+`, `CORE`).
- **El MKV sale con su radiografía hecha**, lista para consultarla al instante.

### ✏️ La radiografía de cada MKV

<img src="docs/img/mkv-radiografia.png" alt="Perfil de luminancia de una película: brillo por fotograma, percentiles y distribución">

- **Todo lo que hay dentro**: audio, subtítulos, HDR10 y Dolby Vision al detalle, desde el perfil y la versión hasta la cadena de mastering.
- **El brillo de la película de principio a fin**, en un gráfico interactivo con sus picos, percentiles y cuánto metraje cae en cada nivel de brillo.
- **Edición instantánea** de nombres de pistas, pistas por defecto y forzadas, y capítulos. Sin recodificar y en segundos; si el MKV está en la biblioteca, se trabaja sobre una copia y el original no se toca.

### 📋 Siempre a la vista

- **La columna de trabajos** acompaña a las tres herramientas: qué se está haciendo, cuánto lleva, cuánto falta y el historial de todo lo terminado.
- **Aviso al terminar** cada trabajo largo, aunque la pestaña esté en segundo plano.
- **La ficha de cada película**, con carátula, sinopsis y puntuación de TMDb.

### 📖 Un manual dentro de la app

<p align="center"><img src="docs/img/manual-cmv4.png" width="720" alt="Manual Dolby Vision CMv4.0 integrado en la aplicación"></p>

Una guía integrada explica Dolby Vision, sus perfiles, la hoja y el repositorio DoviTools y cada una de las rutas del upgrade, con diagramas de qué se hace en cada caso.

### 🌗 A gusto de cada uno

- **Castellano, inglés y catalán**, detectado del navegador la primera vez.
- **Modo claro y oscuro**, que por defecto sigue al del sistema.

---

## Documentación técnica

La descripción completa —todas las variables de configuración, los volúmenes, el pipeline de cada herramienta fase a fase, la API y el desarrollo local— está en **[docs/DOCUMENTACION_TECNICA.md](docs/DOCUMENTACION_TECNICA.md)**.

---

## Agradecimientos

UHD Blu-ray Toolkit se apoya en el trabajo de mucha gente que lo comparte de forma abierta.

- **[R3S3t9999 (REC_9999)](https://github.com/R3S3t9999/DoVi_Scripts)**, autor de DoVi_Scripts, de la hoja DoviTools y del repositorio de ficheros CMv4.0 sobre los que se construye el upgrade. El repositorio lo mantiene una sola persona y se sostiene con donaciones: si te resulta útil, puedes **[apoyarlo en PayPal](https://www.paypal.com/donate/?hosted_button_id=6ML5KUZG9XGB6)**.
- **[dovi_tool](https://github.com/quietvoid/dovi_tool)**, de quietvoid: el análisis, la edición y la inyección de los metadatos Dolby Vision.
- **[MKVToolNix](https://mkvtoolnix.download/)**, de Moritz Bunkus: la lectura de los discos y la creación y edición de los MKV.
- **[FFmpeg](https://ffmpeg.org/)**, en la compilación de **[BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds)**.
- **[MediaInfo](https://mediaarea.net/en/MediaInfo)**, de MediaArea: los detalles técnicos de cada pista.
- **La comunidad del [hilo de Dolby Vision del foro de MakeMKV](https://forum.makemkv.com/forum/viewtopic.php?t=18602)**, donde se ha documentado casi todo lo que hay que saber sobre este tema.
- **[FastAPI](https://fastapi.tiangolo.com/)** y **[Sortable.js](https://sortablejs.github.io/Sortable/)**.

<a href="https://www.themoviedb.org/"><img src="app/static/img/tmdb.svg" alt="The Movie Database (TMDB)" height="18"></a>

Fichas, carátulas y episodios proceden de **[The Movie Database](https://www.themoviedb.org/)**.
This product uses the TMDB API but is not endorsed or certified by TMDB.

---

## Licencia

El código de la aplicación es software libre bajo licencia **MIT** ([`LICENSE`](LICENSE)). La imagen Docker incluye además herramientas de terceros con su propia licencia, entre ellas GPL. El inventario, los avisos y la oferta de código fuente están en [`THIRD-PARTY-NOTICES.md`](THIRD-PARTY-NOTICES.md), y también dentro de la app, en ⚙︎ Configuración → Acerca de.

<p align="center"><img src="docs/img/acerca-de.png" width="640" alt="Licencias y avisos de terceros en la configuración de la app"></p>
