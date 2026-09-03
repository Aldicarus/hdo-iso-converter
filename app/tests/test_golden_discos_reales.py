"""El mapeo de pistas de Fase E, contra los 41 discos reales del NAS.

Las fixtures de `fixtures_discos/` son el recorte de las sesiones de
`/config` (censo del 2026-09-03): las pistas que `mkvmerge -J` reportó del
origen más los `raw` de las pistas incluidas y descartadas. Nada inventado —
si un test de aquí falla, hay un disco de verdad que se procesaría distinto.

Cubre dos escenarios, y la diferencia entre ellos es el motivo del fichero:

* **incluidas solas** — lo que ya corrió en el NAS. Este mapeo NO puede
  cambiar nunca; el golden lo congela.
* **incluidas + todas las descartadas** — simula `recoverTrack`, que es la
  única vía por la que el usuario mete una segunda pista del mismo idioma.
  Ahí es donde el core AC-3 subordinado competía y ganaba.

`_identify_tracks` se EJECUTA (con el mkvmerge falso del arnés), porque el
filtro de cores vive dentro y un test que construyera el `track_map` a mano
no probaría nada.
"""
import asyncio
import json
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.cmv40_harness import FakeToolbox  # noqa: E402
from phases.phase_a import _find_subordinate_track_ids  # noqa: E402
from phases.phase_e import (  # noqa: E402
    _identify_tracks, _match_tracks_to_source,
)

FIXTURES = Path(__file__).parent / "fixtures_discos"
GOLDEN   = Path(__file__).parent / "golden_mapeo_discos.json"


def discos():
    for f in sorted(FIXTURES.glob("*.json")):
        yield json.loads(f.read_text(encoding="utf-8"))


def _pistas(disco, clave, tipo):
    """Reconstruye los objetos que Fase B entrega al matcher. Solo se leen
    `.raw` y `.label`, así que un namespace basta (igual que en
    test_track_mapping)."""
    out = []
    for t in disco.get(clave, []):
        if t.get("track_type") != tipo:
            continue
        r = t["raw"]
        etiqueta = t.get("label") or (
            f"[recuperada] {r['language']} {r['codec']} "
            f"{r.get('description','').split(' /')[0]}"
        )
        out.append(types.SimpleNamespace(
            raw=types.SimpleNamespace(
                language=r["language"], codec=r["codec"],
                description=r.get("description", ""),
                bitrate_kbps=r.get("bitrate_kbps", 0) or 0,
            ),
            label=etiqueta, track_type=tipo,
        ))
    return out


class BaseDiscos(unittest.TestCase):
    """Levanta el arnés una vez y resuelve el track_map de cada disco con el
    `_identify_tracks` de producción."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="golden_discos_"))
        cls.tb = FakeToolbox(cls.tmp)
        cls.tb.install()
        cls.mapas = {}
        for disco in discos():
            nombre = disco["nombre"]
            cls.tb.define_mkv(nombre, tracks=[
                {"id": t["id"], "type": t["type"], "codec": t["codec"],
                 "language": t["properties"].get("language", "und"),
                 "channels": t["properties"].get("audio_channels", 0),
                 "multiplexed_tracks": t["properties"].get("multiplexed_tracks")}
                for t in disco["tracks"]
            ])
            cls.mapas[nombre] = asyncio.run(_identify_tracks(nombre))

    @classmethod
    def tearDownClass(cls):
        cls.tb.uninstall()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def ids(self, tmap, tipo):
        return [i for i, t in sorted(tmap.items()) if t["type"] == tipo]

    def mapear(self, disco, *, recuperar):
        tmap = self.mapas[disco["nombre"]]
        audio = _pistas(disco, "included_tracks", "audio")
        if recuperar:
            audio += _pistas(disco, "discarded_tracks", "audio")
        subs = _pistas(disco, "included_tracks", "subtitle")
        avisos: list[str] = []
        m_audio = _match_tracks_to_source(audio, self.ids(tmap, "audio"), tmap, avisos)
        m_sub = _match_tracks_to_source(subs, self.ids(tmap, "subtitles"), tmap)
        return tmap, audio, subs, m_audio, m_sub, avisos


class TestNingunCoreSubordinado(BaseDiscos):
    """El invariante que ES el arreglo: ninguna pista incluida puede acabar
    apuntando al core AC-3 de un TrueHD.

    Fase A los excluye al construir las `RawAudioTrack`, así que la
    "Castellano DD 5.1" que el usuario ve descartada es la pista
    INDEPENDIENTE. Si Fase E los ve, el core (que suele llevar el ID menor)
    gana y el MKV se lleva el audio lossy que ya está dentro del TrueHD
    mientras la DD 5.1 real no entra.
    """

    def test_los_cores_no_estan_en_el_track_map(self):
        con_core = 0
        for disco in discos():
            tmap = self.mapas[disco["nombre"]]
            cores = _find_subordinate_track_ids(disco["tracks"])
            con_core += bool(cores)
            for cid in cores:
                self.assertNotIn(
                    cid, tmap,
                    f"{disco['nombre']}: el core id {cid} sigue en el track_map",
                )
        self.assertGreaterEqual(con_core, 19, "las fixtures deberían traer cores")

    def test_recuperar_pistas_nunca_entrega_un_core(self):
        for disco in discos():
            cores = _find_subordinate_track_ids(disco["tracks"])
            if not cores:
                continue
            _, audio, _, m_audio, _, _ = self.mapear(disco, recuperar=True)
            for i, sid in m_audio.items():
                self.assertNotIn(
                    sid, cores,
                    f"{disco['nombre']}: «{audio[i].label}» recibe el core id {sid}",
                )

    def test_las_dos_puntas_ven_las_mismas_pistas(self):
        """phase_a y phase_e deben partir de la MISMA base. Cuando no la
        comparten la selección sale bien y el contenido sale cruzado — el bug
        de los subtítulos de tres bloques, otra vez."""
        for disco in discos():
            tmap = self.mapas[disco["nombre"]]
            cores = _find_subordinate_track_ids(disco["tracks"])
            del_disco = {t["id"] for t in disco["tracks"] if t["type"] == "audio"}
            self.assertEqual(
                set(self.ids(tmap, "audio")), del_disco - cores,
                f"{disco['nombre']}: phase_e ve otras pistas de audio que phase_a",
            )


class TestGoldenMapeo(BaseDiscos):
    """Congela el mapeo completo de los 41 discos. Un cambio aquí no es
    necesariamente un error, pero tiene que ser deliberado y revisable: el
    golden guarda el codec y los canales de la pista elegida, no solo su id,
    para que el diff se lea."""

    def _instantanea(self):
        out = {}
        for disco in discos():
            fila = {}
            for etiqueta, recuperar in (("incluidas", False), ("recuperadas", True)):
                tmap, audio, subs, m_audio, m_sub, _ = self.mapear(
                    disco, recuperar=recuperar)
                fila[etiqueta] = [
                    {"pista": audio[i].label, "id": m_audio.get(i),
                     "codec": tmap.get(m_audio.get(i), {}).get("codec"),
                     "canales": tmap.get(m_audio.get(i), {}).get("audio_channels")}
                    for i in range(len(audio))
                ]
            fila["subtitulos"] = [
                {"pista": subs[i].label, "id": m_sub.get(i),
                 "idioma": tmap.get(m_sub.get(i), {}).get("language")}
                for i in range(len(subs))
            ]
            out[disco["nombre"]] = fila
        return out

    def test_mapeo_congelado(self):
        actual = self._instantanea()
        if not GOLDEN.exists():  # pragma: no cover - solo al crearlo
            GOLDEN.write_text(json.dumps(actual, ensure_ascii=False, indent=1),
                              encoding="utf-8")
            self.skipTest("golden creado")
        esperado = json.loads(GOLDEN.read_text(encoding="utf-8"))
        self.assertEqual(sorted(esperado), sorted(actual), "faltan o sobran discos")
        for nombre in esperado:
            self.assertEqual(esperado[nombre], actual[nombre], f"cambia {nombre}")


class TestAvisoDeEmpate(BaseDiscos):
    """Lo que el disco NO permite desambiguar se dice, no se resuelve en
    silencio. Varias AC-3 del mismo idioma y mismos canales (comentarios del
    director) son indistinguibles: mkvmerge devuelve `track_name` vacío en
    todos los orígenes Blu-ray del corpus."""

    def test_te_van_a_matar_avisa_de_las_tres_inglesas(self):
        disco = next(d for d in discos() if d["nombre"].startswith("Te_van_a_matar"))
        _, _, _, _, _, avisos = self.mapear(disco, recuperar=True)
        self.assertTrue(avisos, "debería avisar del empate entre las AC-3 inglesas")
        self.assertTrue(any("indistinguibles" in a for a in avisos), avisos)

    def test_sin_ambiguedad_no_hay_ruido(self):
        """Con solo las incluidas (Castellano + VO) no hay empate en ningún
        disco del corpus: el aviso no debe convertirse en ruido de fondo."""
        for disco in discos():
            _, _, _, _, _, avisos = self.mapear(disco, recuperar=False)
            self.assertEqual(avisos, [], f"{disco['nombre']}: aviso inesperado")


if __name__ == "__main__":
    unittest.main()
