from asyncio import Task
from pathlib import PureWindowsPath, PurePosixPath
from typing import Dict, List

import bsdiff4
import platform

from BaseClasses import ItemClassification as ItemC
from worlds.ucn.DeathlinkMessage import get_deathlink_msg
from .Items import trap_ids, item_data_table
from .Locations import location_table, secret_scares
from CommonClient import *

from Utils import is_windows
from settings import get_settings

class UCNCommandProcessor(ClientCommandProcessor):
    def __init__(self, ctx: CommonContext):
        super().__init__(ctx)

    def _cmd_patch(self):
        """Patches the game"""

        game_path = get_settings()['ucn_options']['game_path']
        with open(game_path, "rb") as file:
            patched_exe = bsdiff4.patch(file.read(), get_patch())
        with open(os.path.join(os.path.dirname(game_path), "UCN Archipelago.exe"), "wb") as file:
            file.write(patched_exe)

        self.output("Successfully patched UCN!")

    def _cmd_change_save_path(self):
        """Change Save Path"""
        save_path = get_settings()['ucn_options']['save_path'].browse()
        get_settings()['ucn_options']['save_path'] = save_path
        self.output(f"Changed to the following directory: {save_path}")

    def _cmd_change_game_path(self):
        """Change Game Path"""
        game_path = get_settings()['ucn_options']['game_path'].browse()
        get_settings()['ucn_options']['game_path'] = game_path
        self.output(f"Changed to the following file: {game_path}")

    def _cmd_check_path(self):
        """Check current save data folder. (Use before connecting!)"""
        self.output(f"Current save path: {get_settings()['ucn_options']}")

    def _cmd_resync(self):
        """Resync your client with the server. (Use if items didn't get added to game, or checks stop being sent. Items are only added if they aren't traps or filler)"""
        self.output("Resyncing client...")
        self.ctx.resync()
        self.output("Client resynced!")

class UCNContext(CommonContext):
    game = "Ultimate Custom Night"
    items_handling = 0b111  # recieve all items
    command_processor = UCNCommandProcessor
    last_scare = ""
    deahlink_start = False

    def __init__(self, server_address=None, password=None):
        super().__init__(server_address, password)
        self.game = "Ultimate Custom Night"
        self.last_item_index = 0
        self.checked_locations: set[int] = set()
        self.target_points = 0
        self.just_connected = False
        self.items_handled = []
        self.deathlink_active = False
        self.dl_deaths_required = 1
        self.dl_progress = 0
        self.traps: Dict[str, int] = {
            f"{trap_id}s_{tracked}" : 0
            for trap_id in trap_ids
            for tracked in ["received", "taken"]
        }

        self.notifications_inqueue: Dict[str, str] = {}

        if not is_windows:
            save_path = get_settings()['ucn_options']['save_path']
            is_windows_path = len(PureWindowsPath(save_path).parts) > len(PurePosixPath(save_path).parts)
            if is_windows_path:
                get_settings()['ucn_options']['save_path'] = '.steam/steam/steamapps/compatdata/871720/pfx/drive_c/users/steamuser/AppData/Roaming/MMFApplications'

            game_path = get_settings()['ucn_options']['game_path']
            is_windows_path = len(PureWindowsPath(game_path).parts) > len(PurePosixPath(game_path).parts)
            if is_windows_path:
                get_settings()['ucn_options']['game_path'] = '.steam/steam/steamapps/common/Ultimate Custom Night/Ultimate Custom Night.exe'

        save_path = get_settings()['ucn_options']['save_path']
        get_settings()['ucn_options']['save_path'] = os.path.abspath(save_path)

        game_path = get_settings()['ucn_options']['game_path']
        get_settings()['ucn_options']['game_path'] = os.path.abspath(game_path)

        self.last_save_time = 0
        self.save_task = asyncio.create_task(self.save_watcher(), name="save_watcher")
        self.notiff_task: Task | None = None
        self.process_task = asyncio.create_task(self.process_watcher(), name="process_watcher")

    async def server_auth(self, password_requested: bool = False):
        if password_requested and not self.password:
            await super().server_auth(password_requested)
        await self.get_username()
        await self.send_connect()

    def on_package(self, cmd, args):
        # Called for every packet after base handling
        if cmd == "Connected":
            if get_settings()['ucn_options']['save_path'] == "":
                raise FileNotFoundError("Save path not set before connecting, use the 'set_path' command and try again")

            self.target_points = args["slot_data"]["target_points"]
            self.just_connected = True
            self.load_client_save(args)
            if args["slot_data"]["vanilla_dd"]:
                self.update_save("randomddactive")

            if args["slot_data"]["death_link"]:
                self.deathlink_active = True
                async_start(self.enable_death_link())

            if args["slot_data"]["dl_amnesty"]:
                self.dl_deaths_required = args["slot_data"]["dl_amnesty"]

        if cmd == "ReceivedItems":
            new_items = list(set(args['items']) - set(self.items_handled))
            for item in new_items:
                self.handle_item(NetworkItem(*item))
            self.watcher_event.set()

    async def enable_death_link(self):
        self.update_save("deathlinkactive")
        await self.update_death_link(True)

    # runs the client GUI
    def run_gui(self):
        from kvui import GameManager

        class UCNManager(GameManager):
            logging_pairs = [
                ("Client", "Archipelago")
            ]
            # TODO update version number every release
            base_title = f"UCN Archipelago Client 0.1.1 | Archipelago"

        self.ui = UCNManager(self)
        self.ui_task = asyncio.create_task(self.ui.async_run(), name="UI")

    # watches the save file
    async def save_watcher(self):
        while not self.exit_event.is_set():
            path = f"{get_settings()['ucn_options']['save_path']}/CNAP"
            if os.path.exists(path):
                mtime = os.path.getmtime(path)
                if self.just_connected or mtime != self.last_save_time:
                    await self.process_save_update()

            await asyncio.sleep(0.5)

    async def process_watcher(self):
        while True:
            if self.save_task is None or self.save_task.done():
                self.save_task = asyncio.create_task(self.save_watcher(), name="save_watcher")

            await asyncio.sleep(30)

    # handles receiving items
    def handle_item(self, item: NetworkItem):
        item_name = self.item_names.lookup_in_game(item.item, "Ultimate Custom Night")
        sender = self.player_names.get(item.player)
        self.items_received.append(NetworkItem(*item))
        game_id = item_data_table[item_name].game_id

        self.notifications_inqueue[item_name] = sender
        if self.notiff_task is None or self.notiff_task.done():
            self.notiff_task = asyncio.create_task(self.send_notification(), name="Notifications")

        if "trap" in game_id:
            self.traps[f"{game_id}s_received"] += 1

            if (self.traps[f"{game_id}s_received"] - self.traps[f"{game_id}s_taken"]) >= 1:
                self.update_save(game_id, item=item)

            if "hippo" in game_id:
                game_id = "mh"
                self.update_save(game_id, item=item)
        else:
            self.update_save(game_id, item=item)

    # handles receiving deathlink
    def on_deathlink(self, data: dict):
        super().on_deathlink(data)
        self.deahlink_start = True
        self.update_save("deathlinkstart")
        async_start(self.deathlink_reset())

    async def deathlink_reset(self):
        await asyncio.sleep(1)
        if self.deahlink_start:
            self.deahlink_start = False
            self.update_save("deathlinkstart", "0")

    # finds new checks cleared and sends them to archipelago
    async def process_save_update(self):
        content = self.read_save()
        # checked_locations: set[int] = set()
        victory = False
        if content is not None:
            # Give starting character
            self.just_connected = False
            self.checked_locations.add(501999)

            await self.queue_received_traps()

            # Read highscore locations
            score = int(content[1].split('=')[1])
            for i in range(int(score / 100)):
                if i <= score:
                    self.checked_locations.add(502000 + i)
                    if (i + 1) * 100 >= self.target_points > 0:
                        victory = True
                        break

            # Read challenge locations
            for i in range(16):
                if f"ch{i + 1}=1" in content:
                    self.checked_locations.add(502100 + i)

            # Read scaresanity locations
            for i in range(56):
                if f"char{i + 1}scare=1" in content:
                    animatronic_id = f"char{i + 1}"
                    self.last_scare = get_animatronic_name(animatronic_id)
                    self.checked_locations.add(location_table[f"Scaresanity: {self.last_scare}"])
                    self.update_save(f"{animatronic_id}scare", "0")

            if self.checked_locations:
                await self.send_msgs([{
                    "cmd": "LocationChecks",
                    "locations": list(self.checked_locations)
                }])
                # self.checked_locations |= new_locations

            # Check for death in game
            if "deathlinkactive=1" in content and "deathlinksend=1" in content:
                self.dl_progress += 1
                self.update_save("deathlinksend", "0")
                if self.dl_progress == self.dl_deaths_required:
                    self.dl_progress = 0
                    await self.send_death(get_deathlink_msg(self.username, self.last_scare))
                else:
                    logger.info(f"Death link progress: {self.dl_progress}/{self.dl_deaths_required}")

                self.update_save("dl_progress", str(self.dl_progress), file="CNAPClient")

            if self.deahlink_start and "deathlinkstart=0" in content:
                self.deahlink_start = False

            if not self.finished_game and victory:
                await self.send_msgs([{"cmd": "StatusUpdate", "status": ClientStatus.CLIENT_GOAL}])
                self.finished_game = True

            self.last_save_time = os.path.getmtime(f"{get_settings()['ucn_options']['save_path']}/CNAP")

    async def queue_received_traps(self):
        content = self.read_save()

        for trap_id in trap_ids:
            if f"{trap_id}=0" in content and (self.traps[f"{trap_id}s_received"] - self.traps[f"{trap_id}s_taken"]) > 0:
                self.traps[f"{trap_id}s_taken"] += 1
                self.update_save(f"{trap_id}staken", str(self.traps[f"{trap_id}s_taken"]), file="CNAPClient")

                if f"{trap_id}=0" in content and (self.traps[f"{trap_id}s_received"] - self.traps[f"{trap_id}s_taken"]) >= 1:
                    await asyncio.sleep(15)
                    self.update_save(trap_id)

    # adds received items to the game
    def update_save(self, game_id: str, new_value="1", item: NetworkItem = None, file="CNAP"):
        ucn_save = self.read_save(file)
        if ucn_save is not None:
            new_item = True
            for line in ucn_save:
                if game_id in line:
                    new_item = False
                    # Handle Power Up items
                    if game_id[1].isnumeric():
                        ucn_save[ucn_save.index(line)] = f"{game_id}={int(ucn_save[ucn_save.index(line)][3]) + 1}"

                    elif line != f"{game_id}={new_value}":
                        ucn_save[ucn_save.index(line)] = f"{game_id}={new_value}"
                    break

            item_handled = False

            while True:
                try:
                    if new_item:
                        with open(f"{get_settings()['ucn_options']['save_path']}/{file}", 'a') as fa:
                            fa.write(f"\n{game_id}={new_value}")
                            item_handled = True
                    elif file == "CNAP" and (len(ucn_save) < 3 or "ep=13" not in ucn_save[2]):
                        self.reset_game_save()
                        self.resync()
                        break
                    else:
                        with open(f"{get_settings()['ucn_options']['save_path']}/{file}", 'w') as fw:
                            fw.write("\n".join(ucn_save))
                            item_handled = True

                except PermissionError:
                    item_handled = False

                if item_handled:
                    if item is not None:
                        self.items_handled.append(item)
                    break

    async def send_notification(self):
        while len(self.notifications_inqueue) > 0:
            content = self.read_save()

            if "queueitemmessage=1" not in content:
                new_item = list(self.notifications_inqueue)[0]
                self.update_save("recieveditem", new_item)
                self.update_save("givenplayer", self.notifications_inqueue[new_item])
                self.update_save("queueitemmessage")
                self.notifications_inqueue.pop(new_item)
            await asyncio.sleep(3)

    # Loads client save data when rejoining a lobby
    def load_client_save(self, args):
        while True:
            try:
                with open(f"{get_settings()['ucn_options']['save_path']}/CNAPClient", 'r') as f:
                    lines = [line.rstrip() for line in f.readlines()]
                break
            except PermissionError:
                pass
            except FileNotFoundError:
                self.reset_saves(args)
                return

        if not os.path.exists(f"{get_settings()['ucn_options']['save_path']}/CNAP"):
            self.reset_saves(args)
            return

        game_save = self.read_save()

        if "seed_id" in lines[0] and args["slot_data"]["seed_identifier"] not in lines[0].split("=")[1] or \
          len(game_save) < 3 or "ep=13" not in game_save[2]:
            self.reset_saves(args)
            return

        for line in lines:
            if "trapstaken" in line:
                self.traps[f"{line.split("taken")[0]}_taken"] = int(line.split("=")[1]) + 1
            if "dl_progress" in line:
                self.dl_progress = int(line.split("=")[1])

    # Resets the saves for repeat playtroughs
    def reset_saves(self, args):
        self.reset_game_save()
        self.reset_client_save(args)

        if self.save_task is None or self.save_task.done():
            self.save_task = asyncio.create_task(self.save_watcher(), name="save_watcher")

    def reset_game_save(self):
        while True:
            try:
                with open(f"{get_settings()['ucn_options']['save_path']}/CNAP", 'w') as f:
                    # ep=13: only the golden freddy cutscene plays at 9800 hs, adjust=1: skips adjust volume screen
                    f.write("[CNAP]\n"
                            "hs=0\n"
                            "ep=13\n"
                            "adjust=1\n"
                            f"targetpoints={self.target_points}")
                    
                if self.deathlink_active:
                    with open(f"{get_settings()['ucn_options']['save_path']}/CNAP", 'a') as f:
                        f.write("\ndeathlinkactive=1")
                break
            except PermissionError:
                pass

    def reset_client_save(self, args):
        while True:
            try:
                with open(f"{get_settings()['ucn_options']['save_path']}/CNAPClient", 'w') as f:
                    f.write(f"seed_id={args["slot_data"]["seed_identifier"]}")
                break
            except PermissionError:
                pass

    def read_save(self, file="CNAP") -> List[str] | None:
        try:
            with open(f"{get_settings()['ucn_options']['save_path']}/{file}", 'r') as fr:
                save = [line.rstrip() for line in fr.readlines()]
            return save
        except PermissionError:
            return None

    def resync(self):
        for item in self.items_received:
            item_name = self.item_names.lookup_in_game(item.item, "Ultimate Custom Night")
            if item_data_table[item_name].type != ItemC.progression or item_data_table[item_name].type != ItemC.useful:
                self.update_save(item_data_table[item_name].game_id)

        if self.save_task is None or self.save_task.done():
            self.save_task = asyncio.create_task(self.save_watcher(), name="save_watcher")

def get_animatronic_name(scare_id) -> str:
    game_id = scare_id.split("scare")[0]
    for animatronic in item_data_table.keys():
        if game_id in item_data_table[animatronic].game_id:
            return animatronic

    for animatronic in secret_scares.keys():
        if game_id in secret_scares[animatronic]:
            return animatronic

    return ""

def get_patch():
    import pkgutil
    patch_name = 'Gamejolt.Patch.bsdiff' if get_settings()['ucn_options']['game_path'].is_gamejolt else 'UCNAP.bsdiff'
    return pkgutil.get_data(__name__, f"/patch/{patch_name}")


def main():
    Utils.init_logging("UCNClient", exception_logger="Client")

    async def _main():
        parser = get_base_parser("FNaF UCN Client")
        args = parser.parse_args()

        ctx = UCNContext(args.connect, args.password)
        ctx.server_task = asyncio.create_task(server_loop(ctx), name="server loop")

        if gui_enabled:
            ctx.run_gui()
        ctx.run_cli()

        await ctx.exit_event.wait()
        await ctx.shutdown()

    import colorama
    colorama.init()

    asyncio.run(_main())
    colorama.deinit()


if __name__ == "__main__":
    main()
