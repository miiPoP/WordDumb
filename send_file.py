#!/usr/bin/env python3
import shutil
from pathlib import Path

from calibre.gui2 import FunctionDispatcher
from calibre_plugins.worddumb.config import prefs
from calibre_plugins.worddumb.database import get_ll_path, get_x_ray_path


class SendFile():
    def __init__(self, gui, data):
        self.gui = gui
        self.device_manager = self.gui.device_manager
        (self.book_id, _, self.asin, self.book_path, self.mi) = data
        self.ll_path = get_ll_path(self.asin, self.book_path)
        self.x_ray_path = get_x_ray_path(self.asin, self.book_path)
        self.retry = False

    # use some code from calibre.gui2.device:DeviceMixin.upload_books
    def send_files(self, job):
        if job is not None:
            self.device_manager.add_books_to_metadata(
                job.result, [self.mi], self.gui.booklists())
            if not self.gui.set_books_in_library(
                    self.gui.booklists(), reset=True,
                    add_as_step_to_job=job, do_device_sync=False):
                self.gui.upload_booklists(job)
            self.gui.refresh_ondevice()
            view = self.gui.memory_view
            view.model().resort(reset=False)
            view.model().research()

        [has_book, _, _, _, paths] = self.gui.book_on_device(self.book_id)
        device_info = self.device_manager.get_current_device_information()
        # /Volumes/Kindle
        device_path_prefix = device_info['info'][4]['main']['prefix']
        if has_book:
            device_book_path = Path(device_path_prefix)
            device_book_path = device_book_path.joinpath(next(iter(paths)))
            self.move_file_to_device(self.ll_path, device_book_path)
            if prefs['x-ray']:
                self.move_file_to_device(self.x_ray_path, device_book_path)
        elif not self.retry:
            # upload book and cover to device
            cover_path = Path(self.book_path).parent.joinpath('cover.jpg')
            self.mi.thumbnail = None, None, cover_path.read_bytes()
            book_name = Path(self.book_path).name
            titles = [i.title for i in [self.mi]]
            plugboards = self.gui.current_db.new_api.pref('plugboards', {})
            self.device_manager.upload_books(
                FunctionDispatcher(self.send_files), [self.book_path],
                [book_name], on_card=None, metadata=[self.mi],
                titles=titles, plugboards=plugboards)
            self.retry = True

    def move_file_to_device(self, file_path, device_book_path):
        file_folder = device_book_path.stem + '.sdr'
        device_file_path = device_book_path.parent.joinpath(file_folder)
        if not device_file_path.is_dir():
            device_file_path.mkdir()
        device_file_path = device_file_path.joinpath(file_path.name)
        if device_file_path.is_file():
            device_file_path.unlink()
        shutil.move(file_path, device_file_path)


def send(gui, data):
    sf = SendFile(gui, data)
    sf.send_files(None)


def kindle_connected(gui):
    if not gui.device_manager.is_device_connected:
        return False
    device = gui.device_manager.device
    if device.VENDOR_ID != [0x1949]:  # Kindle device
        return False
    return True
