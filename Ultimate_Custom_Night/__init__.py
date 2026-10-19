import random
import os
from typing import BinaryIO

from .Regions import region_data_table
from .Rules import set_scorerules, set_scaresanity_rules, set_challenge_rules
from worlds.generic.Rules import exclusion_rules
from BaseClasses import Location, Region, Entrance, Tutorial, Item, ItemClassification
from .Options import UCNOptions
from worlds.AutoWorld import World, WebWorld
from worlds.LauncherComponents import Component, components, Type, launch_subprocess
from multiprocessing import Process
from typing import List, Mapping, Any, Dict
from Options import OptionError
import settings

from BaseClasses import Region, Tutorial
from worlds.AutoWorld import WebWorld, World

from .Regions import region_data_table
from .Items import (
    UcnItem,
    UcnItemData,
    item_table,
    item_data_table,
    challenge_items,
    mandatory_items,
    trap_items,
    severe_traps,
    powerup_items
)
from .RuleDataTables import animatronic_rule_table, challenge_rule_table, hard_logic_table

from .Locations import advancement_table, location_table, secret_scares


def start_client():
    from .Client import main
    launch_subprocess(main, name="UCNClient")


components.append(Component("FNAF UCN Client", func=start_client, component_type=Type.CLIENT))

class UcnSettings(settings.Group):
    class GamePath(settings.UserFilePath):
        """The path to game exe"""
        description = '"Ultimate Custom Night.exe"'
        steam_md5 = '8cdac7efc631d81387c64833901f5e80'
        gamejolt_md5 = 'c4cc0cde9956622975b577401b4812ab'

        is_gamejolt = False

        @classmethod
        def _validate_stream_hashes(cls, f: BinaryIO) -> None:
            """Helper to efficiently validate stream against hashes
            and determine gamejolt vs steam"""
            pos = f.tell()
            try:
                from hashlib import md5
                file_md5 = md5()
                block = bytearray(64*1024)
                view = memoryview(block)
                while n := f.readinto(view):  # type: ignore
                    file_md5.update(view[:n])
                file_md5_hex = file_md5.hexdigest()

                if cls.gamejolt_md5 == file_md5_hex:
                    cls.is_gamejolt = True
                elif cls.steam_md5 == file_md5_hex:
                    cls.is_gamejolt = False
                else:
                    raise ValueError(f'Hashes do not match for {cls.__name__}')
            finally:
                f.seek(pos)

    class SavePath(settings.UserFolderPath):
        """The path to the save folder, named \"MMFApplications\""""
        description = '"MMFApplications" (usually in %appdata%)'
        
        @classmethod
        def validate(cls, path: str) -> None:
            if os.path.basename(os.path.abspath(path)) != 'MMFApplications':
                raise ValueError(f'Selected path "{path}" is not "MMFApplications"')

    game_path: GamePath = GamePath('C:/Program Files (x86)/Steam/steamapps/common/Ultimate Custom Night/Ultimate Custom Night.exe')
    save_path: SavePath = SavePath('AppData/Roaming/MMFApplications')


class UcnLocation(Location):
    game = "Ultimate Custom Night"


class UcnWeb(WebWorld):
    tutorials = [Tutorial(
        "Multiworld Setup Tutorial",
        "A guide to setting up the Archipelago Ultimate Custom Night software on your computer. This guide covers "
        "single-player, multiworld, and related software.",
        "English",
        "setup_en.md",
        "setup/en",
        ["Fewffwa"]
    )]


class UcnWorld(World):
    """
    Welcome to the ultimate FNAF mashup, featuring 50 selectable characters and custom difficulties!
    """
    game = "Ultimate Custom Night"
    options_dataclass = UCNOptions
    options = UCNOptions
    web = UcnWeb()
    seed_id = []
    excluded_locations = []
    excluded_items = []
    gmb_in_pool = True
    location_name_to_id = location_table
    item_name_to_id = item_table
    settings: UcnSettings

    max_points = 100

    data_version = 1

    # Add items to the itempool
    def create_items(self) -> None:
        items_to_add: List[str] = []

        items_to_add.extend(mandatory_items)

        if self.options.challenge_sanity.value:
            items_to_add.extend(challenge_items)

        # Remove items from the item pool based on options
        items_to_add = [item for item in items_to_add if item not in self.excluded_items]

        # if self.max_points < self.options.target_points.value:
        #     raise OptionError(f"Highscore target is higher than the maximum possible score! Lower target score by {
        #     self.options.target_points.value - self.max_points} and try again!")

        starting_character = random.choice([animatronic for animatronic, ability in
                                            animatronic_rule_table.items() if ability is None and
                                            animatronic not in self.options.exclude_animatronics.value])

        self.multiworld.get_location("Starting Character", self.player).place_locked_item(
            self.create_item(items_to_add.pop(items_to_add.index(starting_character)))
        )

        item_pool = [self.create_item(name) for name in items_to_add]

        empty_locations = len(self.multiworld.get_unfilled_locations(self.player))
        remaining_items_needed = empty_locations - len(item_pool)
        item_pool += self.create_filler_items(remaining_items_needed)

        self.multiworld.itempool += item_pool

    def create_filler_items(self, item_slots: int) -> List[UcnItem]:
        filler_items: List[UcnItem] = []
        if self.options.vanilla_dd:
            powerup_items.append("DeeDee Repel")

        for _ in range(item_slots):
            if random.randint(1, 100) <= self.options.trap_chance.value:
                chosen_item = random.choice(trap_items)
                if random.randint(1, 100) <= self.options.trap_chance_ex.value:
                    chosen_item = random.choice(severe_traps)
            else:
                chosen_item = random.choice(powerup_items)

            filler_items.append(self.create_item(chosen_item))

        return filler_items

    def set_rules(self):
        if self.options.hard_logic.value:
            animatronic_rule_table.update(hard_logic_table)

        target_points = self._get_score_locations() * 100
        set_scorerules(self.multiworld, self.player, target_points, bool(self.options.hard_logic.value), self.gmb_in_pool)

        if self.options.challenge_sanity.value:
            set_challenge_rules(self.multiworld, self.player, self.excluded_locations)

        if self.options.scare_sanity.value:
            set_scaresanity_rules(self.multiworld, self.player, self.excluded_locations)

        location = self.multiworld.get_location(f"{target_points}pts", self.player)
        self.multiworld.completion_condition[self.player] = lambda state: state.can_reach(location)

    def generate_early(self) -> None:
        pass

    def generate_basic(self) -> None:
        self.seed_id += [random.randint(0, 50210) for _ in range(7)]

    def create_regions(self) -> None:
        self.exclude_locations()

        # Create regions.
        for region_name in region_data_table.keys():
            region = Region(region_name, self.player, self.multiworld)
            self.multiworld.regions.append(region)

        # Create locations.
        target_points = self._get_score_locations()

        for region_name, region_data in region_data_table.items():
            region = self.multiworld.get_region(region_name, self.player)
            locations = {
                location_name: location_data.address for location_name, location_data in advancement_table.items()
                if location_data.region == region_name and location_data.can_create(self.multiworld, self.player) and
                   location_data.address <= 501999 + target_points
            }

            if self.options.challenge_sanity.value:
                locations.update({
                    location_name: location_data.address for location_name, location_data in advancement_table.items()
                    if
                    location_data.region == region_name and location_data.can_create(self.multiworld, self.player) and
                    "Challenge" in location_name and location_name not in self.excluded_locations
                })

            if self.options.scare_sanity.value:
                locations.update({
                    location_name: location_data.address for location_name, location_data in advancement_table.items()
                    if
                    location_data.region == region_name and location_data.can_create(self.multiworld, self.player) and
                    "Scare" in location_name and location_name not in self.excluded_locations
                })

            region.add_locations(locations, UcnLocation)
            region.add_exits(region_data_table[region_name].connecting_regions)

        # Place locked locations.
        for location_name, location_data in locked_locations.items():
            # Ignore locations we never created.
            if not location_data.can_create(self.multiworld, self.player):
                continue

            locked_item = self.create_item(advancement_table[location_name].locked_item)
            self.multiworld.get_location(location_name, self.player).place_locked_item(locked_item)

    def exclude_locations(self):
        removed_items = 0
        if self.options.scare_sanity.value and self.options.death_link.value:
            raise OptionError("Scaresanity can't be used with death link enabled! Turn off either death link, or scaresanity to continue")

        # TODO Implement when animatronic packs are added
        # if self.options.hard_logic and len(self.options.exclude_animatronics.value):
        #     raise OptionError("You can't exclude animatronics with hard logic enabled. You wanted it hard, you got it hard.")

        for animatronic in self.options.exclude_animatronics.value:
            removed_items += self.exclude_animatronic(animatronic)

        while 68 - self.options.target_points.value > removed_items:
            random_animatronic = random.choice(list(animatronic_rule_table.keys()))
            if random_animatronic not in self.excluded_items:
                removed_items += self.exclude_animatronic(random_animatronic)

        if "Bonnie" in self.excluded_items and "Foxy" not in self.excluded_items:
            animatronic_rule_table["Foxy"] = None

        if self.max_points < 100:
            for animatronic in secret_scares:
                self.excluded_locations.append(f"Scaresanity: {animatronic}")

        if "Global Music Box" in self.excluded_items:
            self.gmb_in_pool = False

    def exclude_animatronic(self, animatronic: str):
        removed_items = 0
        if animatronic not in animatronic_rule_table.keys():
            raise OptionError(
                f"Non Animatronic item '{animatronic}' in the exclusion list! Remove this item from the list!"
            )

        required_abilities = animatronic_rule_table[animatronic]
        if required_abilities is not None:
            for ability in required_abilities:
                anims_using_ability = [anim for anim, abl in animatronic_rule_table.items() if
                                       abl is not None and ability in abl]

                if ability not in self.excluded_items and all(
                        anim in self.options.exclude_animatronics.value for anim in anims_using_ability):
                    self.excluded_items.append(ability)
                    removed_items += 1

        if self.options.challenge_sanity.value:
            for challenge in challenge_rule_table.keys():
                if challenge not in self.excluded_items and animatronic in challenge_rule_table[challenge]:
                    self.excluded_items.append(challenge)
                    self.excluded_locations.append(f"Challenge: {challenge}")

                    # Exclude Bear's Attack 3, cause there is no challenge key for it to iterate over
                    if "Attack 2" in challenge:
                        self.excluded_items.append("Bears Attack 3")
                        self.excluded_locations.append(f"Challenge: Bears Attack 3")

                    # Exclude Pay Attention 1/2, cause there is no challenge key for it to iterate over
                    if "Attention" in challenge:
                        self.excluded_items.append("Pay Attention 1")
                        self.excluded_items.append("Pay Attention 2")
                        self.excluded_locations.append(f"Challenge: Pay Attention 1")
                        self.excluded_locations.append(f"Challenge: Pay Attention 2")

        if self.options.scare_sanity.value:
            self.excluded_locations.append(f"Scaresanity: {animatronic}")

        self.excluded_items.append(animatronic)
        removed_items += 1
        self.max_points -= 2

        return removed_items

    def fill_slot_data(self) -> Dict[str, object]:
        target_points = self._get_score_locations() * 100
        # raise OptionError(f"Target Points:{target_points}")
        return {
            "seed_identifier": "".join(str(i) for i in self.seed_id),
            "target_points": target_points,
            "vanilla_dd": bool(self.options.vanilla_dd.value),
            "death_link": bool(self.options.death_link.value),
            "dl_amnesty": self.options.dl_amnesty.value
        }

    def create_item(self, name: str) -> UcnItem:
        return UcnItem(name, item_data_table[name].type, item_data_table[name].code, self.player)

    def create_event(self, name: str, classification: ItemClassification) -> Item:
        return UcnItem(None, classification, None, self.player)

    def _get_score_locations(self) -> int:
        target_points = self.options.target_points.value
        if self.max_points < target_points:
            return self.max_points
        return target_points
locked_locations = {name: data for name, data in advancement_table.items() if data.locked_item}
