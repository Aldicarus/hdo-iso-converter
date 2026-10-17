"""MediaInfo lista el núcleo AC-3 de un TrueHD como pista propia, con el
MISMO `ID` (PID) que el TrueHD. La Fase A lo quita antes de emparejar las
pistas de MediaInfo con las de mkvmerge —que ya quitó el core con
`multiplexed_tracks`—, y el emparejado es POSICIONAL: sin ese filtro, desde el
primer core cada pista de audio recibe el bitrate, el formato comercial y el
layout de canales de la anterior. Ningún test lo fijaba (mutación P2-6 de la auditoría
del 2026-10-17).

Ejecutar desde la raíz del repo:
    python3 -m unittest app.tests.test_mediainfo_sin_cores -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models import (  # noqa: E402
    BDInfoResult, MediaInfoResult, MediaInfoTrack, RawAudioTrack,
)
from phases.phase_a import enrich_tracks_with_mediainfo  # noqa: E402


def _bd():
    pistas = [RawAudioTrack(language="Spanish", codec="Dolby TrueHD/Atmos Audio",
                            description="7.1 / 48 kHz", bitrate_kbps=0),
              RawAudioTrack(language="English", codec="DTS-HD Master Audio",
                            description="5.1 / 48 kHz", bitrate_kbps=0)]
    return BDInfoResult(video_tracks=[], audio_tracks=pistas, subtitle_tracks=[],
                        duration_seconds=7200.0, has_fel=False, vo_language="English")


def _mediainfo():
    crudo = {"media": {"track": [
        {"@type": "General"},
        {"@type": "Audio", "ID": "4352", "Format": "MLP FBA"},
        {"@type": "Audio", "ID": "4352", "Format": "AC-3"},       # el núcleo
        {"@type": "Audio", "ID": "4353", "Format": "DTS"},
    ]}}
    pistas = [MediaInfoTrack(track_type="audio", bitrate_kbps=4500,
                             format_commercial="Dolby TrueHD with Dolby Atmos"),
              MediaInfoTrack(track_type="audio", bitrate_kbps=640,
                             format_commercial="Dolby Digital"),
              MediaInfoTrack(track_type="audio", bitrate_kbps=3500,
                             format_commercial="DTS-HD Master Audio")]
    return MediaInfoResult(tracks=pistas, raw_json=crudo)


class TestElNucleoAc3NoDesplazaLasPistas(unittest.TestCase):

    def test_cada_pista_recibe_lo_suyo(self):
        bd = _bd()
        enrich_tracks_with_mediainfo(bd, _mediainfo())
        self.assertEqual(
            [(t.language, t.format_commercial, t.bitrate_kbps) for t in bd.audio_tracks],
            [("Spanish", "Dolby TrueHD with Dolby Atmos", 4500),
             ("English", "DTS-HD Master Audio", 3500)],
            "la pista inglesa ha recibido los datos del núcleo AC-3")


if __name__ == "__main__":
    unittest.main()
