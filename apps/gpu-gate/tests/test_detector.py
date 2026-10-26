import builtins
import contextlib
import io
import os
import unittest
from unittest import mock

from gpu_gate import procfs
from gpu_gate.detector import Detector

from .fakes import Tree

PROTON = "/home/u/.local/share/Steam/steamapps/common/Proton - Experimental/files/lib/wine"
REAPER_ARGV = ["/home/u/.local/share/Steam/ubuntu12_32/reaper", "SteamLaunch", "AppId=1817070",
               "--", "/home/u/.local/share/Steam/steamapps/common/SteamLinuxRuntime_4/_v2-entry-point"]


class DetectorTest(unittest.TestCase):
    def setUp(self):
        self.t = Tree()
        # always-present desktop noise that must never count as a game
        self.t.proc_(1, "systemd", argv=["/usr/lib/systemd/systemd"], ppid=0)
        self.t.proc_(2, "kthreadd", ppid=0)
        self.t.proc_(300, "steam", argv=["/home/u/.local/share/Steam/ubuntu12_32/steam"])
        self.t.proc_(301, "steamwebhelper", argv=["./steamwebhelper"], ppid=300)
        self.t.proc_(400, "java", argv=["/usr/bin/java", "-jar", "/opt/jenkins.war"])
        self.t.proc_(500, "ollama", argv=["/usr/local/bin/ollama", "serve"])

    def tearDown(self):
        self.t.cleanup()

    def detect(self, games=(), ignore=()):
        return Detector(games, ignore).detect(procfs.scan(self.t.proc))

    def add_proton_game(self, appid="1817070"):
        """The real process tree of a Proton game (Spider-Man, observed 2026-10-26 via stat ppid).
        environ and exe are unreadable (fixture default), as for another user's processes."""
        argv = list(REAPER_ARGV)
        argv[2] = "AppId=" + appid
        self.t.proc_(2000, "reaper", argv=argv, ppid=300)
        self.t.proc_(2001, "srt-bwrap", argv=["/x/srt-bwrap"], ppid=2000)
        self.t.proc_(2002, "pv-adverb", argv=["/x/pv-adverb"], ppid=2001)
        self.t.proc_(2003, "python3", argv=["python3"], ppid=2002)
        self.t.proc_(2004, "steam.exe", argv=["c:\\windows\\system32\\steam.exe"], ppid=2003)
        self.t.proc_(2005, "wineserver", argv=[PROTON + "/../../bin/wineserver"], ppid=2004)
        self.t.proc_(2006, "services.exe", argv=["C:\\windows\\system32\\services.exe"], ppid=2004)
        self.t.proc_(2010, "Spider-Man.exe", ppid=2004,
                     argv=["S:\\steamapps\\common\\Marvel's Spider-Man Remastered\\Spider-Man.exe"])
        self.t.proc_(2011, "crs-handler.exe", argv=["S:\\x\\crs-handler.exe"], ppid=2010)

    def test_no_game(self):
        self.assertIsNone(self.detect())

    def test_steam_reaper_cmdline_names_the_game_under_it(self):
        self.add_proton_game()
        self.assertEqual(self.detect(), ("Spider-Man.exe", "steam"))

    def test_steam_reaper_with_only_plumbing_reports_app_id(self):
        self.t.proc_(2000, "reaper", argv=REAPER_ARGV, ppid=300)
        self.t.proc_(2001, "srt-bwrap", argv=["/x/srt-bwrap"], ppid=2000)
        self.assertEqual(self.detect(), ("steam app 1817070", "steam"))

    def test_steam_native_game(self):
        self.t.proc_(2000, "reaper", ppid=300, argv=[
            "/s/reaper", "SteamLaunch", "AppId=440", "--", "/g/hl2.sh", "-game", "tf"])
        self.t.proc_(2001, "hl2_linux", argv=["/g/hl2_linux", "-game", "tf"], ppid=2000)
        self.assertEqual(self.detect(), ("hl2_linux", "steam"))

    def test_steam_app_id_zero_garbage_and_after_separator_ignored(self):
        self.t.proc_(2000, "reaper", argv=["/s/reaper", "SteamLaunch", "AppId=0", "--"])
        self.t.proc_(2001, "reaper", argv=["/s/reaper", "SteamLaunch", "AppId=abc", "--"])
        self.t.proc_(2002, "reaper", argv=["/s/reaper", "SteamLaunch", "--", "AppId=7"])
        self.t.proc_(2003, "reaper", argv=["/s/reaper", "AppId=7"])  # no SteamLaunch
        self.t.proc_(2004, "notreaper", argv=["/x/notreaper", "SteamLaunch", "AppId=7"])
        self.assertIsNone(self.detect())

    def test_steam_environ_alone_does_not_count(self):
        # 12.9: environ is never read, even when (as for our own uid) it would be readable
        self.t.proc_(2100, "game", argv=["/g/game"], environ={"SteamAppId": "440"})
        self.assertIsNone(self.detect())

    def test_never_opens_exe_or_environ(self):
        """Every way of opening or resolving a path is spied: builtins.open, io.open (and with
        it pathlib), io.FileIO, os.open, os.readlink. Only comm, cmdline and stat may be read."""
        self.add_proton_game()
        self.t.proc_(2400, "java", argv=["java", "net.minecraft.client.main.Main"])
        touched = []

        def spy(real):
            def wrapper(path, *a, **kw):
                touched.append(os.fsdecode(path) if isinstance(path, (str, bytes, os.PathLike))
                               else repr(path))
                return real(path, *a, **kw)
            return wrapper

        targets = {"builtins.open": builtins.open, "io.open": io.open, "io.FileIO": io.FileIO,
                   "os.open": os.open, "os.readlink": os.readlink}
        with contextlib.ExitStack() as stack:
            for name, real in targets.items():
                stack.enter_context(mock.patch(name, side_effect=spy(real)))
            for rules in ((), ("Spider",)):
                self.assertIsNotNone(Detector(rules).detect(procfs.scan(self.t.proc)))
        self.assertTrue(touched)
        bad = [p for p in touched if os.path.basename(p) not in ("comm", "cmdline", "stat")]
        self.assertEqual(bad, [])

    def test_wine_without_steam(self):
        self.t.proc_(2200, "wineserver", argv=["/usr/bin/wineserver"])
        self.t.proc_(2201, "Celeste.exe", argv=["Z:\\games\\Celeste.exe"])
        self.assertEqual(self.detect(), ("Celeste.exe", "wine"))

    def test_wine_only_plumbing_reports_plumbing(self):
        self.t.proc_(2200, "wineserver", argv=["/usr/bin/wineserver"])
        self.assertEqual(self.detect(), ("wineserver", "wine"))

    def test_exe_comm_truncated_uses_argv0(self):
        self.t.proc_(2200, "AVeryLongGameNa", argv=["C:\\Games\\AVeryLongGameName.exe"])
        self.assertEqual(self.detect(), ("AVeryLongGameName.exe", "wine"))

    def test_gamescope(self):
        self.t.proc_(2300, "gamescope-wl", argv=["gamescope", "-W", "2560", "--"])
        self.assertEqual(self.detect(), ("gamescope-wl", "gamescope"))

    def test_minecraft_vanilla_and_not_other_java(self):
        self.assertIsNone(self.detect())  # jenkins java in setUp
        self.t.proc_(2400, "java", argv=[
            "java", "-cp", "/home/u/.minecraft/libraries/x.jar", "net.minecraft.client.main.Main"])
        self.assertEqual(self.detect(), ("minecraft", "minecraft"))

    def test_minecraft_prism_instance(self):
        self.t.proc_(2400, "java", argv=[
            "/usr/lib/jvm/java-21/bin/java",
            "-Djava.library.path=/home/u/.local/share/PrismLauncher/instances/0.5/natives",
            "org.prismlauncher.EntryPoint"])
        self.assertEqual(self.detect(), ("minecraft (0.5)", "minecraft"))

    def test_games_allow_list_wins_first(self):
        self.t.proc_(2500, "factorio", argv=["/opt/factorio/bin/x64/factorio"])
        self.add_proton_game()
        self.assertEqual(self.detect(games=["Factorio"]), ("factorio", "games"))
        self.assertEqual(self.detect(), ("Spider-Man.exe", "steam"))

    def test_ignore_list(self):
        self.t.proc_(2200, "wineserver", argv=["/usr/bin/wineserver"])
        self.t.proc_(2201, "Notepad++.exe", argv=["C:\\npp\\Notepad++.exe"])
        self.assertEqual(self.detect(ignore=["notepad++"]), ("wineserver", "wine"))
        self.assertIsNone(self.detect(ignore=["notepad++", "wine"]))

    def test_ignore_skips_steam_descendant_name_and_reaper(self):
        self.add_proton_game()
        self.assertEqual(self.detect(ignore=["spider"]), ("steam app 1817070", "steam"))
        self.assertEqual(self.detect(ignore=["reaper"]), ("Spider-Man.exe", "wine"))

    def test_ignore_beats_allow(self):
        self.t.proc_(2500, "factorio", argv=["/opt/factorio"])
        self.assertIsNone(self.detect(games=["factorio"], ignore=["factorio"]))

    def test_vanished_and_unreadable_processes(self):
        self.t.proc_(2600, "ghost.exe", argv=["C:\\ghost.exe"])
        os.remove(os.path.join(self.t.proc, "2600", "comm"))  # exited between listdir and read
        os.mkdir(os.path.join(self.t.proc, "2601"))  # empty pid dir
        self.t.proc_(2602, "reaper", argv=REAPER_ARGV)
        os.remove(os.path.join(self.t.proc, "2602", "stat"))  # no ppid: still detected
        self.assertEqual(self.detect(), ("steam app 1817070", "steam"))

    def test_parse_ppid_with_parens_in_comm(self):
        self.assertEqual(procfs.parse_ppid("42 (a) b) c) S 7 42 42 0"), 7)
        self.assertIsNone(procfs.parse_ppid("garbage"))
        self.assertIsNone(procfs.parse_ppid(None))


if __name__ == "__main__":
    unittest.main()
