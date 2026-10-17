"""La Fase H comprueba el MKV que se entrega, no lo que se pretendía meter.

La auditoría de la suite con Codex (2026-10-17) encontró que ninguna de las
dos rutas miraba el fichero final:

  · la **rápida** (drop-in) construía el resultado a mano —`DoviInfo(profile=7,
    el_type="FEL", cm_version="v4.0")`— y su chequeo de frames solo
    registraba la diferencia, así que un MKV truncado se entregaba;
  · la **completa** (merge) valida el RPU del HEVC PREVIO al mux, así que un
    remux que tomara el vídeo del MKV origen —la mutación P3-2, que sobrevivía
    a la suite entera— pasaba en verde.

Hoy las dos muestrean 30 s del MKV entregado y comprueban el perfil, la capa
y la versión CM de lo que lleva de verdad, y la rápida falla con más de dos
frames de diferencia cuando el recuento lo da `ffprobe`.

Y tres fallos más de la misma fase y de la Fase A, verificados leyendo el
código:

  · un `import time` local dejaba `time` sin definir en la copia entre
    sistemas de ficheros cuando el RPU venía adelantado de la Fase G;
  · el RPU extraído del stream se borraba ANTES del precache de Tab 2, que
    acababa usando el inyectado (CLAUDE.md: «se prefiere el RPU EXTRAÍDO»);
  · si ffmpeg fallaba en la Fase A, el `source.hevc` a medias se quedaba, y
    el reintento lo reutilizaba si llegaba al 50 % del MKV.

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_cmv40_fase_h_mira_el_mkv -v
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))
sys.path.insert(0, str(APP_DIR / "tests"))

from cmv40_harness import (  # noqa: E402
    PhaseTestCase, RpuProps, make_session, write_artifacts,
)

FRAMES = 1000
SRC_FEL = RpuProps(profile=7, el_type="FEL", cm_version="v2.9", frames=FRAMES)
SRC_MEL = RpuProps(profile=7, el_type="MEL", cm_version="v2.9", frames=FRAMES)
TGT_FEL = RpuProps(profile=7, el_type="FEL", cm_version="v4.0", frames=FRAMES,
                   has_l8=True)
TGT_MEL = RpuProps(profile=7, el_type="MEL", cm_version="v4.0", frames=FRAMES,
                   has_l8=True)
INJ_FEL = RpuProps(profile=7, el_type="FEL", cm_version="v4.0", frames=FRAMES,
                   has_l8=True)
INJ_P8 = RpuProps(profile=8, el_type="", cm_version="v4.0", frames=FRAMES,
                  has_l8=True)


class FaseHCase(PhaseTestCase):

    def preparar(self, *, workflow="p7_fel", target_type="trusted_p7_fel_final",
                 trust=True, source=SRC_FEL, target=TGT_FEL, artefactos=(),
                 inyectado=INJ_FEL):
        self.tb.define_rpu("RPU_source.bin", **source.as_dict())
        self.tb.define_rpu("RPU_target.bin", **target.as_dict())
        write_artifacts(self.wd, "RPU_source.bin", props=source)
        write_artifacts(self.wd, "RPU_target.bin", props=target)
        for nombre in artefactos:
            write_artifacts(self.wd, nombre, props=inyectado)
        s = make_session(self.wd, source_workflow=workflow,
                         target_type=target_type, target_trust_ok=trust,
                         source_frame_count=FRAMES, target_frame_count=FRAMES)
        write_artifacts(self.wd, "source.mkv", props=source)
        self.tb.define_media("source.mkv", duration=7200.0, frames=FRAMES)
        return s

    def mkv_de_la_fase_g(self, s, props, *, frames=FRAMES):
        """El `.mkv.tmp` que la Fase G habría escrito, con el RPU que lleva."""
        tmp = self.output_dir / f"{s.output_mkv_name}.tmp"
        write_artifacts(self.output_dir, tmp.name, props=props)
        self.tb.define_media(tmp.name, duration=7200.0, frames=frames)
        return tmp

    async def fase_h(self, s):
        from phases.cmv40_pipeline import run_phase_h_validate
        return await run_phase_h_validate(s, self.log)

    def entregado(self, s):
        return (self.output_dir / s.output_mkv_name).exists()


class TestLaRutaRapidaMideElMkv(FaseHCase):

    async def test_un_mkv_truncado_no_se_entrega(self):
        s = self.preparar(artefactos=("source_injected.hevc",))
        tmp = self.mkv_de_la_fase_g(s, INJ_FEL, frames=FRAMES // 2)
        with self.assertRaises(RuntimeError) as ctx:
            await self.fase_h(s)
        self.assertIn(str(FRAMES // 2), str(ctx.exception))
        self.assertFalse(self.entregado(s), "un MKV con la mitad de frames se ha entregado")
        self.assertTrue(tmp.exists(), "el .mkv.tmp se conserva para inspeccionarlo")

    async def test_dos_frames_de_diferencia_se_toleran(self):
        """El redondeo de `ffprobe` sobre un MKV (ver la nota de nb_frames)."""
        s = self.preparar(artefactos=("source_injected.hevc",))
        self.mkv_de_la_fase_g(s, INJ_FEL, frames=FRAMES - 2)
        await self.fase_h(s)
        self.assertTrue(self.entregado(s))

    async def test_si_ffprobe_no_da_el_recuento_no_se_inventa_un_fallo(self):
        """Sin `nb_frames` el recuento es una estimación por duración, y una
        estimación no puede tumbar la fase: solo se registra."""
        s = self.preparar(artefactos=("source_injected.hevc",))
        self.mkv_de_la_fase_g(s, INJ_FEL, frames=None)
        await self.fase_h(s)
        self.assertTrue(self.entregado(s))

    async def test_el_dv_del_resultado_sale_del_mkv(self):
        s = self.preparar(artefactos=("source_injected.hevc",))
        self.mkv_de_la_fase_g(s, INJ_FEL)
        r = await self.fase_h(s)
        self.assertEqual((r["profile"], r["el_type"], r["cm_version"]), (7, "FEL", "v4.0"))
        muestreos = [c for c in self.tb.find("ffmpeg")
                     if any(a.endswith(".mkv.tmp") for a in c.argv)]
        self.assertTrue(muestreos, "no se ha leído el MKV que se entrega")

    async def test_un_mkv_sin_cmv40_no_se_entrega(self):
        s = self.preparar(artefactos=("source_injected.hevc",))
        self.mkv_de_la_fase_g(s, SRC_FEL)        # el vídeo del origen, CMv2.9
        with self.assertRaises(RuntimeError) as ctx:
            await self.fase_h(s)
        self.assertIn("v2.9", str(ctx.exception))
        self.assertFalse(self.entregado(s))

    async def test_el_muestreo_del_mkv_son_treinta_segundos(self):
        """Sin el `-t`, ffmpeg volcaría el HEVC entero —decenas de GB— para
        que dovi_tool lea 720 frames (revisión de Codex de la suite)."""
        s = self.preparar(artefactos=("source_injected.hevc",))
        self.mkv_de_la_fase_g(s, INJ_FEL)
        await self.fase_h(s)
        [m] = [c for c in self.tb.find("ffmpeg") if any(a.endswith(".mkv.tmp") for a in c.argv)]
        self.assertIsNotNone(m.opt("-t"), m.argv)
        self.assertLessEqual(float(m.opt("-t")), 30)
        self.assertEqual(m.opt("-c:v"), "copy", "recodificar pierde el RPU")

    async def test_no_extrae_el_rpu_entero(self):
        """El muestreo son 30 s: la ruta rápida sigue sin pagar el
        `extract-rpu` completo que justifica la otra."""
        s = self.preparar(artefactos=("source_injected.hevc",))
        self.mkv_de_la_fase_g(s, INJ_FEL)
        await self.fase_h(s)
        for c in self.tb.find("dovi_tool", "extract-rpu"):
            self.assertIn("--limit", c.argv)


class TestLaRutaMergeMiraElMkvEntregado(FaseHCase):

    async def test_un_mkv_con_el_video_del_origen_no_se_entrega(self):
        """El HEVC previo al mux está bien y el MKV lleva el vídeo del
        origen: la validación del RPU completo lo daba por bueno."""
        s = self.preparar(trust=False, artefactos=("BL.hevc", "DV_dual.hevc"))
        self.mkv_de_la_fase_g(s, SRC_FEL)
        with self.assertRaises(RuntimeError):
            await self.fase_h(s)
        self.assertFalse(self.entregado(s))

    async def test_un_single_layer_con_profile_7_no_se_entrega(self):
        """«Te van a matar»: la pista decía P8.1 y MediaInfo leía dvhe.07."""
        s = self.preparar(workflow="p7_mel", target_type="trusted_p7_mel_final",
                          source=SRC_MEL, target=TGT_MEL,
                          artefactos=("BL_injected.hevc",), inyectado=INJ_P8)
        self.mkv_de_la_fase_g(s, RpuProps(profile=7, el_type="MEL", cm_version="v4.0",
                                          frames=FRAMES, has_l8=True))
        with self.assertRaises(RuntimeError) as ctx:
            await self.fase_h(s)
        self.assertIn("7", str(ctx.exception))
        self.assertFalse(self.entregado(s))

    async def test_un_mkv_truncado_tampoco_por_merge(self):
        """El HEVC previo al mux está ENTERO —su RPU da los frames buenos— y el
        `.mkv.tmp` lleva media película: un remux interrumpido que el
        forward-roll dio por bueno. La rama merge solo miraba el HEVC; ahora
        compara el vídeo del MKV con el del origen (auditoría con Codex del
        2026-10-17)."""
        s = self.preparar(trust=False, artefactos=("BL.hevc", "DV_dual.hevc"))
        tmp = self.mkv_de_la_fase_g(s, INJ_FEL, frames=FRAMES // 2)
        with self.assertRaises(RuntimeError) as ctx:
            await self.fase_h(s)
        self.assertIn(str(FRAMES // 2), str(ctx.exception))
        self.assertFalse(self.entregado(s))
        self.assertTrue(tmp.exists(), "el .mkv.tmp se conserva para inspeccionarlo")

    async def test_el_merge_completo_se_entrega(self):
        s = self.preparar(trust=False, artefactos=("BL.hevc", "DV_dual.hevc"))
        self.mkv_de_la_fase_g(s, INJ_FEL)
        await self.fase_h(s)
        self.assertTrue(self.entregado(s))
        self.assertIn("1000 frames", self.log.text)

    async def test_el_single_layer_bien_hecho_se_entrega(self):
        s = self.preparar(workflow="p7_mel", target_type="trusted_p7_mel_final",
                          source=SRC_MEL, target=TGT_MEL,
                          artefactos=("BL_injected.hevc",), inyectado=INJ_P8)
        self.mkv_de_la_fase_g(s, INJ_P8)
        await self.fase_h(s)
        self.assertTrue(self.entregado(s))


class TestLaRutaMergeValidaElStreamCompleto(FaseHCase):
    """Dos mutaciones que sobrevivían a la suite entera."""

    async def test_un_rpu_con_frames_DE_MAS_tampoco_pasa(self):
        """La diferencia es absoluta: un RPU más largo que el vídeo es tan
        incorrecto como uno recortado."""
        largo = RpuProps(profile=7, el_type="FEL", cm_version="v4.0",
                         frames=FRAMES + 50, has_l8=True)
        s = self.preparar(trust=False, artefactos=("BL.hevc", "DV_dual.hevc"),
                          inyectado=largo)
        self.mkv_de_la_fase_g(s, INJ_FEL)
        with self.assertRaises(RuntimeError) as ctx:
            await self.fase_h(s)
        self.assertIn(str(FRAMES + 50), str(ctx.exception))

    async def test_con_el_dual_y_la_el_valida_el_dual(self):
        """En p7_fel con merge existen a la vez la EL inyectada y el dual que
        salió del `dovi_tool mux`, y el que se multiplexó es el dual. Validar
        la EL daría por bueno un mux que hubiera salido recortado."""
        s = self.preparar(trust=False, artefactos=("BL.hevc",))
        write_artifacts(self.wd, "EL_injected.hevc", props=INJ_FEL)
        write_artifacts(self.wd, "DV_dual.hevc", props=RpuProps(
            profile=7, el_type="FEL", cm_version="v4.0", frames=FRAMES // 2,
            has_l8=True))
        self.mkv_de_la_fase_g(s, INJ_FEL)
        with self.assertRaises(RuntimeError):
            await self.fase_h(s)


class TestLaFaseHLiberaLosHevc(FaseHCase):
    """Validado y movido el MKV, los HEVC del workdir no sirven para nada: en
    un UHD son ~227 GB por job. Lo vigilaba un test que buscaba el nombre de
    los ficheros en el fuente, y seguía en verde con el `unlink` comentado."""

    HEVCS = ("source.hevc", "source_injected.hevc", "DV_dual.hevc",
             "EL_injected.hevc", "BL_injected.hevc")

    async def test_tras_entregar_no_queda_ningun_hevc(self):
        s = self.preparar(trust=False, artefactos=("BL.hevc", "DV_dual.hevc"))
        for nombre in self.HEVCS:
            write_artifacts(self.wd, nombre, props=INJ_FEL)
        self.mkv_de_la_fase_g(s, INJ_FEL)
        await self.fase_h(s)
        quedan = [n for n in self.HEVCS if (self.wd / n).exists()]
        self.assertEqual(quedan, [], "la Fase H no ha liberado los HEVC")
        self.assertTrue((self.wd / "RPU_target.bin").exists(),
                        "lo que no es HEVC se queda: un rehacer lo necesita")

    async def test_si_la_validacion_falla_no_borra_nada(self):
        s = self.preparar(trust=False, artefactos=("BL.hevc", "DV_dual.hevc"))
        write_artifacts(self.wd, "source.hevc", props=SRC_FEL)
        self.mkv_de_la_fase_g(s, SRC_FEL)          # no lleva CMv4.0
        with self.assertRaises(RuntimeError):
            await self.fase_h(s)
        self.assertTrue((self.wd / "DV_dual.hevc").exists())
        self.assertTrue((self.wd / "source.hevc").exists())


class TestLaFaseGYLaHEnCadena(FaseHCase):
    """La Fase G de verdad y después la H, con el `mkvmerge` falso
    construyendo el MKV a partir de su argv. Así el remux que se equivoca de
    entrada —el vídeo del origen, o el origen sin su audio— no llega al
    nombre final."""

    PISTAS_ORIGEN = [
        {"type": "video", "codec": "HEVC/H.265/MPEG-H", "dimensions": "3840x2160"},
        {"type": "audio", "codec": "TrueHD Atmos", "language": "spa", "channels": 8},
        {"type": "audio", "codec": "TrueHD Atmos", "language": "eng", "channels": 8},
        {"type": "subtitles", "codec": "HDMV PGS", "language": "spa", "forced": True},
        {"type": "subtitles", "codec": "HDMV PGS", "language": "spa"},
    ]

    async def _g_y_h(self, **kw):
        from phases.cmv40_pipeline import run_phase_g_remux
        s = self.preparar(**kw)
        self.tb.define_mkv("source.mkv", tracks=self.PISTAS_ORIGEN)
        await run_phase_g_remux(s, self.log)
        # Sin `define_media` para la salida: el recuento que da `ffprobe` es
        # el del MKV que construyó el mux, no uno declarado a mano. Con el
        # declarado, un mux que truncara el vídeo pasaba en verde.
        return s, await self.fase_h(s)

    async def test_drop_in(self):
        s, r = await self._g_y_h(artefactos=("source_injected.hevc",))
        self.assertTrue(self.entregado(s))
        self.assertEqual(r["cm_version"], "v4.0")

    async def test_merge(self):
        s, r = await self._g_y_h(trust=False, artefactos=("BL.hevc", "EL_injected.hevc"))
        self.assertTrue(self.entregado(s))
        self.assertEqual(r["cm_version"], "v4.0")

    async def test_un_hevc_truncado_da_un_mkv_que_no_se_entrega(self):
        """De punta a punta: un HEVC con la mitad de los frames, el mux de
        verdad de la Fase G y la ruta rápida de la H, que tiene que pararlo
        con el recuento del propio MKV (revisión de Codex de la suite)."""
        corto = RpuProps(**dict(INJ_FEL.as_dict(), frames=FRAMES // 2))
        with self.assertRaises(RuntimeError) as ctx:
            await self._g_y_h(artefactos=("source_injected.hevc",), inyectado=corto)
        self.assertIn(str(FRAMES // 2), str(ctx.exception))

    async def test_un_mkv_sin_el_audio_del_origen_no_se_entrega(self):
        """La Fase H cuenta las pistas: un remux que dejara fuera el audio o
        los subtítulos del origen salía en verde."""
        s = self.preparar(artefactos=("source_injected.hevc",))
        self.tb.define_mkv("source.mkv", tracks=self.PISTAS_ORIGEN)
        tmp = self.mkv_de_la_fase_g(s, INJ_FEL)
        self.tb.define_mkv(tmp.name, tracks=self.PISTAS_ORIGEN[:1])
        with self.assertRaises(RuntimeError) as ctx:
            await self.fase_h(s)
        self.assertIn("0", str(ctx.exception))
        self.assertFalse(self.entregado(s))

    async def test_el_mkv_entregado_lleva_las_pistas_del_origen(self):
        s, _ = await self._g_y_h(artefactos=("source_injected.hevc",))
        import json
        import subprocess
        out = subprocess.run(["mkvmerge", "-J", str(self.output_dir / s.output_mkv_name)],
                             capture_output=True, text=True).stdout
        tipos = [t["type"] for t in json.loads(out)["tracks"]]
        self.assertEqual(tipos, ["video", "audio", "audio", "subtitles", "subtitles"])


class TestElRpuExtraidoLlegaAlPrecache(FaseHCase):
    """Se borraba en el `finally` de la validación, antes del precache."""

    async def test_el_adelantado_por_la_fase_g(self):
        s = self.preparar(trust=False, artefactos=("BL.hevc", "DV_dual.hevc"))
        self.mkv_de_la_fase_g(s, INJ_FEL)
        write_artifacts(self.wd, "_validate_full_rpu.bin", props=INJ_FEL)
        await self.fase_h(s)
        self.assertTrue((self.wd / "_validate_full_rpu.bin").exists(),
                        "el RPU extraído del stream no sobrevive a la Fase H")
        exports = [c.opt("-i") for c in self.tb.find("dovi_tool", "export")]
        self.assertTrue(any(str(e).endswith("_validate_full_rpu.bin") for e in exports),
                        f"el precache no ha leído el RPU extraído: {exports}")

    async def test_el_extraido_en_la_propia_fase(self):
        s = self.preparar(trust=False, artefactos=("BL.hevc", "DV_dual.hevc"))
        self.mkv_de_la_fase_g(s, INJ_FEL)
        await self.fase_h(s)
        exports = [c.opt("-i") for c in self.tb.find("dovi_tool", "export")]
        self.assertTrue(any(str(e).endswith("_validate_full_rpu.bin") for e in exports),
                        f"el precache no ha leído el RPU extraído: {exports}")


class TestLaCopiaEntreSistemasDeFicheros(FaseHCase):
    """Proyectos antiguos con el MKV en el workdir: `os.rename` falla con
    EXDEV y se copia. Con el RPU adelantado por la Fase G, el `import time`
    local de la función no se ejecutaba y `time` quedaba sin definir."""

    async def test_con_el_rpu_adelantado_termina(self):
        import errno
        s = self.preparar(trust=False, artefactos=("BL.hevc", "DV_dual.hevc"))
        self.mkv_de_la_fase_g(s, INJ_FEL)
        write_artifacts(self.wd, "_validate_full_rpu.bin", props=INJ_FEL)
        with self._entre_dispositivos():
            await self.fase_h(s)
        self.assertTrue(self.entregado(s))
        self.assertTrue(self.copiado, "el escenario no ha pasado por la copia")
        self.assertEqual(list(self.output_dir.glob("*.part")), [])

    def _entre_dispositivos(self):
        """`os.rename` y `os.link` del `.mkv.tmp` hacia /mnt/output fallan
        con EXDEV, como entre dos filesystems."""
        import contextlib
        import errno
        from phases import cmv40_pipeline
        rename, link = cmv40_pipeline.os.rename, cmv40_pipeline.os.link
        self.copiado = False

        def _cruzado(origen, destino):
            return (str(destino).startswith(str(self.output_dir))
                    and str(origen).endswith(".mkv.tmp"))

        def _rename(origen, destino):
            if _cruzado(origen, destino):
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            return rename(origen, destino)

        def _link(origen, destino):
            if _cruzado(origen, destino):
                self.copiado = True
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            return link(origen, destino)

        pila = contextlib.ExitStack()
        pila.enter_context(mock.patch.object(cmv40_pipeline.os, "rename", _rename))
        pila.enter_context(mock.patch.object(cmv40_pipeline.os, "link", _link))
        return pila

    async def test_cancelar_a_mitad_no_deja_nada_con_el_nombre_final(self):
        """`shutil.move` daba al destino su nombre DEFINITIVO desde el primer
        byte, y su hilo seguía copiando —y borraba el origen— con la fase ya
        cancelada (auditoría con Codex del 2026-10-17)."""
        import asyncio
        import builtins
        import time as _time
        s = self.preparar(trust=False, artefactos=("BL.hevc", "DV_dual.hevc"))
        tmp = self.mkv_de_la_fase_g(s, INJ_FEL)
        write_artifacts(self.wd, "_validate_full_rpu.bin", props=INJ_FEL)
        from phases import cmv40_pipeline
        abrir = builtins.open

        class _Lento:
            def __init__(self, f):
                self._f = f

            def read(self, n=-1):
                _time.sleep(0.2)
                return self._f.read(n)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                self._f.close()

        def _open(ruta, modo="r", *a, **k):
            f = abrir(ruta, modo, *a, **k)
            return _Lento(f) if str(ruta) == str(tmp) and "r" in modo else f

        with self._entre_dispositivos(), \
                mock.patch.object(cmv40_pipeline, "_TROZO_COPIA", 1024), \
                mock.patch.object(cmv40_pipeline, "open", _open, create=True):
            tarea = asyncio.create_task(self.fase_h(s))
            parcial = self.output_dir / f"{s.output_mkv_name}.part"
            for _ in range(200):
                if parcial.exists():
                    break
                await asyncio.sleep(0.02)
            self.assertTrue(parcial.exists(), "la copia no ha empezado")
            tarea.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await tarea
            for _ in range(50):
                if not parcial.exists():
                    break
                await asyncio.sleep(0.05)
        self.assertFalse(self.entregado(s), "un MKV a medias con el nombre final")
        self.assertFalse(parcial.exists(), "la copia siguió tras cancelar")
        self.assertTrue(tmp.exists(), "se borró el origen con la fase cancelada")


class TestLaPublicacionNoPisaNada(FaseHCase):
    """`os.rename` sustituye el destino. Entre la comprobación «no existe» y
    el rename, un fichero que apareciera —otra copia del NAS— se perdía sin
    un error (auditoría con Codex del 2026-10-17)."""

    async def test_un_fichero_que_aparece_en_el_ultimo_momento(self):
        s = self.preparar(trust=False, artefactos=("BL.hevc", "DV_dual.hevc"))
        self.mkv_de_la_fase_g(s, INJ_FEL)
        write_artifacts(self.wd, "_validate_full_rpu.bin", props=INJ_FEL)
        final = self.output_dir / s.output_mkv_name
        from phases import cmv40_pipeline
        link, rename = cmv40_pipeline.os.link, cmv40_pipeline.os.rename

        def _aparece(origen, destino):
            if str(destino) == str(final) and not final.exists():
                final.write_bytes(b"de otro")
            return link(origen, destino)

        def _aparece_r(origen, destino):
            if str(destino) == str(final) and not final.exists():
                final.write_bytes(b"de otro")
            return rename(origen, destino)

        with mock.patch.object(cmv40_pipeline.os, "link", _aparece), \
                mock.patch.object(cmv40_pipeline.os, "rename", _aparece_r):
            with self.assertRaises(RuntimeError):
                await self.fase_h(s)
        self.assertEqual(final.read_bytes(), b"de otro", "la Fase H pisó un fichero ajeno")


class TestFaseANoDejaElHevcAMedias(PhaseTestCase):
    """Si ffmpeg falla, el `source.hevc` a medias se borra: el reintento lo
    reutilizaba si pasaba del 50 % del tamaño del MKV."""

    async def test_un_ffmpeg_fallido_no_deja_source_hevc(self):
        from phases.cmv40_pipeline import run_phase_a_analyze_source
        s = make_session(self.wd)
        write_artifacts(self.wd, "source.mkv", props=SRC_FEL)
        self.tb.define_media("source.mkv", duration=7200.0, frames=FRAMES)
        self.tb.define_rpu("RPU_source.bin", **SRC_FEL.as_dict())
        # El camino del pipe no es viable (forzado) y el de dos pasos escribe
        # y DESPUÉS falla, como un ffmpeg que muere a mitad.
        self.tb.fail_when_arg("ffmpeg", "tee")
        self.tb.fail_when_arg("ffmpeg", "source.hevc", rc=1,
                              stderr="Error writing trailer\n", tras_producir=True)
        with self.assertRaises(RuntimeError):
            await run_phase_a_analyze_source(s, log_callback=self.log)
        self.assertFalse((self.wd / "source.hevc").exists(),
                         "el HEVC a medias se queda para que el reintento lo reutilice")



class TestUnSegundoDisparoPorLaRamaMerge(FaseHCase):
    """La Fase H puede dispararse dos veces (los dos disparadores del
    auto-pipeline), y el segundo llega con el MKV ya en su nombre definitivo.
    Revalidar tiene que ser idempotente también por la rama merge, que tras
    el primer éxito ha borrado los HEVC previos al mux."""

    async def test_el_segundo_revalida_sin_fallar(self):
        s = self.preparar(trust=False, artefactos=("BL.hevc", "DV_dual.hevc"))
        self.mkv_de_la_fase_g(s, INJ_FEL)
        await self.fase_h(s)
        self.assertTrue(self.entregado(s))
        r = await self.fase_h(s)
        self.assertTrue(r.get("already_validated"), r)
        self.assertTrue(self.entregado(s))

    async def test_sin_el_rpu_extraido_comprueba_el_propio_mkv(self):
        """Un proyecto anterior a que el RPU extraído sobreviviera: ni HEVC ni
        RPU, y el MKV en su sitio. Se revalida lo que hay, y se dice."""
        s = self.preparar(trust=False, artefactos=())
        final = self.output_dir / s.output_mkv_name
        write_artifacts(self.output_dir, final.name, props=INJ_FEL)
        self.tb.define_media(final.name, duration=7200.0, frames=FRAMES)
        r = await self.fase_h(s)
        self.assertTrue(r.get("already_validated"))
        self.assertTrue(any("_sniff_final" in " ".join(c.argv)
                            for c in self.tb.find("ffmpeg")))

    async def test_en_la_primera_pasada_sin_hevc_sigue_fallando(self):
        s = self.preparar(trust=False, artefactos=())
        self.mkv_de_la_fase_g(s, INJ_FEL)
        with self.assertRaises(RuntimeError):
            await self.fase_h(s)
        self.assertFalse(self.entregado(s))


if __name__ == "__main__":
    unittest.main()
