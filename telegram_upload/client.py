import getpass
import json
import re
from distutils.version import StrictVersion
from typing import Iterable, Union
from urllib.parse import urlparse

import click
import os

from telethon.network import ConnectionTcpMTProxyRandomizedIntermediate
from telethon.tl.types import Message, DocumentAttributeFilename
from telethon.utils import pack_bot_file_id

from telegram_upload.config import SESSION_FILE
from telegram_upload.exceptions import ThumbError, TelegramUploadDataLoss, TelegramUploadNoSpaceError, \
    TelegramProxyError, TelegramInvalidFile, MissingFileError
from telegram_upload.files import get_file_attributes, get_file_thumb, File
from telethon.version import __version__ as telethon_version
from telethon import TelegramClient

from telegram_upload.utils import free_disk_usage, sizeof_fmt, grouper

if StrictVersion(telethon_version) >= StrictVersion('1.0'):
    import telethon.sync


CAPTION_MAX_LENGTH = 200
ALBUM_FILES = 10
PROXY_ENVIRONMENT_VARIABLE_NAMES = [
    'TELEGRAM_UPLOAD_PROXY',
    'HTTPS_PROXY',
    'HTTP_PROXY',
]


def phone_match(value):
    match = re.match(r'\+?[0-9.()\[\] \-]+', value)
    if match is None:
        raise ValueError('{} is not a valid phone'.format(value))
    return value


def get_progress_bar(action, file, length):
    bar = click.progressbar(label='{} "{}"'.format(action, file), length=length)

    def progress(current, total):
        bar.pos = 0
        bar.update(current)
    return progress, bar


def truncate(text, max_length):
    return (text[:max_length - 3] + '...') if len(text) > max_length else text


def get_proxy_environment_variable():
    for env_name in PROXY_ENVIRONMENT_VARIABLE_NAMES:
        if env_name in os.environ:
            return os.environ[env_name]


def parse_proxy_string(proxy: Union[str, None]):
    if not proxy:
        return None
    proxy_parsed = urlparse(proxy)
    if not proxy_parsed.scheme or not proxy_parsed.hostname or not proxy_parsed.port:
        raise TelegramProxyError('Malformed proxy address: {}'.format(proxy))
    if proxy_parsed.scheme == 'mtproxy':
        return ('mtproxy', proxy_parsed.hostname, proxy_parsed.port, proxy_parsed.username)
    try:
        import socks
    except ImportError:
        raise TelegramProxyError('pysocks module is required for use HTTP/socks proxies. '
                                 'Install it using: pip install pysocks')
    proxy_type = {
        'http': socks.HTTP,
        'socks4': socks.SOCKS4,
        'socks5': socks.SOCKS5,
    }.get(proxy_parsed.scheme)
    if proxy_type is None:
        raise TelegramProxyError('Unsupported proxy type: {}'.format(proxy_parsed.scheme))
    return (proxy_type, proxy_parsed.hostname, proxy_parsed.port, True,
            proxy_parsed.username, proxy_parsed.password)


class Client(TelegramClient):
    def __init__(self, config_file, proxy=None, **kwargs):
        with open(config_file) as f:
            config = json.load(f)
        proxy = proxy if proxy is not None else get_proxy_environment_variable()
        proxy = parse_proxy_string(proxy)
        if proxy and proxy[0] == 'mtproxy':
            proxy = proxy[1:]
            kwargs['connection'] = ConnectionTcpMTProxyRandomizedIntermediate
        super().__init__(config.get('session', SESSION_FILE), config['api_id'], config['api_hash'],
                         proxy=proxy, **kwargs)

    def start(
            self,
            phone=lambda: click.prompt('Please enter your phone', type=phone_match),
            password=lambda: getpass.getpass('Please enter your password: '),
            *,
            bot_token=None, force_sms=False, code_callback=None,
            first_name='New User', last_name='', max_attempts=3):
        return super().start(phone=phone, password=password, bot_token=bot_token, force_sms=force_sms,
                             first_name=first_name, last_name=last_name, max_attempts=max_attempts)

    def send_files_as_album(self, entity, files, delete_on_success=False, print_file_id=False,
                            force_file=False, forward=(), caption=None, thumbnail=None):
        for files_group in grouper(ALBUM_FILES, files):
            self._send_album(entity, files_group)

    def send_files(self, entity, files: Iterable[File], delete_on_success=False, print_file_id=False,
                   force_file=False, forward=(), caption=None):
        has_files = False
        for file in files:
            has_files = True
            progress, bar = get_progress_bar('Uploading', file.file_name, file.file_size)
            file_caption = truncate(caption if caption is not None else file.short_name, CAPTION_MAX_LENGTH)
            thumb = file.get_thumbnail()
            try:
                if force_file or isinstance(file, File):
                    attributes = [DocumentAttributeFilename(file.file_name)]
                else:
                    attributes = get_file_attributes(file)
                try:
                    message = self.send_file(entity, file, thumb=thumb,
                                             file_size=file.file_size if isinstance(file, File) else None,
                                             caption=file_caption, force_document=force_file,
                                             progress_callback=progress, attributes=attributes)
                    if hasattr(message.media, 'document') and file.file_size != message.media.document.size:
                        raise TelegramUploadDataLoss(
                            'Remote document size: {} bytes (local file size: {} bytes)'.format(
                                message.media.document.size, file.file_size))
                finally:
                    bar.render_finish()
            finally:
                if thumb and file.is_custom_thumbnail:
                    os.remove(thumb)
            if print_file_id:
                click.echo('Uploaded successfully "{}" (file_id {})'.format(file.file_name,
                                                                            pack_bot_file_id(message.media)))
            if delete_on_success:
                click.echo('Deleting "{}"'.format(file))
                os.remove(file.path)
            self.forward_to(message, forward)
        if not has_files:
            raise MissingFileError('Files do not exist.')

    def find_files(self, entity):
        for message in self.iter_messages(entity):
            if message.document:
                yield message
            else:
                break

    def download_files(self, entity, messages: Iterable[Message], delete_on_success: bool = False):
        messages = reversed(list(messages))
        for message in messages:
            filename_attr = next(filter(lambda x: isinstance(x, DocumentAttributeFilename),
                                        message.document.attributes), None)
            filename = filename_attr.file_name if filename_attr else 'Unknown'
            if message.document.size > free_disk_usage():
                raise TelegramUploadNoSpaceError(
                    'There is no disk space to download "{}". Space required: {}'.format(
                        filename, sizeof_fmt(message.document.size - free_disk_usage())
                    )
                )
            progress, bar = get_progress_bar('Downloading', filename, message.document.size)
            try:
                self.download_media(message, progress_callback=progress)
            finally:
                bar.render_finish()
            if delete_on_success:
                self.delete_messages(entity, [message])

    def forward_to(self, message, destinations):
        for destination in destinations:
            self.forward_messages(destination, [message])
