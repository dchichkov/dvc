import errno
import os

import configobj
import pytest
from mock import patch

from dvc.config import Config
from dvc.exceptions import DownloadError, UploadError
from dvc.main import main
from dvc.path_info import PathInfo
from dvc.remote import LocalRemote
from dvc.remote.base import BaseRemote, RemoteCacheRequiredError
from dvc.compat import fspath
from dvc.utils.fs import remove
from tests.basic_env import TestDvc
from tests.remotes import Local


class TestRemote(TestDvc):
    def test(self):
        remotes = ["a", "b", "c"]

        self.assertEqual(main(["remote", "list"]), 0)
        self.assertNotEqual(main(["remote", "remove", remotes[0]]), 0)

        for r in remotes:
            self.assertEqual(
                main(["remote", "add", "--default", r, "s3://bucket/name"]), 0
            )

        self.assertEqual(main(["remote", "list"]), 0)

        self.assertEqual(
            main(["remote", "modify", remotes[0], "checksum_jobs", "1"]), 0
        )
        self.assertEqual(main(["remote", "remove", remotes[0]]), 0)

        self.assertEqual(main(["remote", "list"]), 0)

    def test_relative_path(self):
        dname = os.path.join("..", "path", "to", "dir")
        ret = main(["remote", "add", "mylocal", dname])
        self.assertEqual(ret, 0)

        # NOTE: we are in the repo's root and config is in .dvc/, so
        # dir path written to config should be just one level above.
        rel = os.path.join("..", dname)
        config = configobj.ConfigObj(self.dvc.config.files["repo"])
        self.assertEqual(
            config['remote "mylocal"']["url"], rel.replace("\\", "/")
        )

    def test_overwrite(self):
        remote_name = "a"
        remote_url = "s3://bucket/name"
        self.assertEqual(main(["remote", "add", remote_name, remote_url]), 0)
        self.assertEqual(main(["remote", "add", remote_name, remote_url]), 251)
        self.assertEqual(
            main(["remote", "add", "-f", remote_name, remote_url]), 0
        )

    def test_referencing_other_remotes(self):
        assert main(["remote", "add", "foo", "ssh://localhost/"]) == 0
        assert main(["remote", "add", "bar", "remote://foo/dvc-storage"]) == 0

        config = configobj.ConfigObj(self.dvc.config.files["repo"])
        assert config['remote "bar"']["url"] == "remote://foo/dvc-storage"


def test_remove_default(tmp_dir, dvc):
    remote = "mys3"
    assert (
        main(["remote", "add", "--default", remote, "s3://bucket/name"]) == 0
    )
    assert main(["remote", "modify", remote, "profile", "default"]) == 0
    assert main(["config", "--local", "core.remote", remote]) == 0

    config = configobj.ConfigObj(dvc.config.files["repo"])
    local_config = configobj.ConfigObj(dvc.config.files["local"])
    assert config["core"]["remote"] == remote
    assert local_config["core"]["remote"] == remote

    assert main(["remote", "remove", remote]) == 0

    config = configobj.ConfigObj(dvc.config.files["repo"])
    local_config = configobj.ConfigObj(dvc.config.files["local"])
    assert config.get("core", {}).get("remote") is None
    assert local_config.get("core", {}).get("remote") is None


class TestRemoteRemove(TestDvc):
    def test(self):
        ret = main(["config", "core.checksum_jobs", "1"])
        self.assertEqual(ret, 0)

        remote = "mys3"
        ret = main(["remote", "add", remote, "s3://bucket/name"])
        self.assertEqual(ret, 0)

        ret = main(["remote", "remove", remote])
        self.assertEqual(ret, 0)


class TestRemoteDefault(TestDvc):
    def test(self):
        remote = "mys3"
        ret = main(["remote", "add", "mys3", "s3://bucket/path"])
        self.assertEqual(ret, 0)

        ret = main(["remote", "default", "mys3"])
        self.assertEqual(ret, 0)
        config_file = os.path.join(self.dvc.dvc_dir, Config.CONFIG)
        config = configobj.ConfigObj(config_file)
        default = config["core"]["remote"]
        self.assertEqual(default, remote)

        ret = main(["remote", "default", "--unset"])
        self.assertEqual(ret, 0)
        config = configobj.ConfigObj(config_file)
        default = config.get("core", {}).get("remote")
        self.assertEqual(default, None)


def test_show_default(dvc, capsys):
    assert main(["remote", "default", "foo"]) == 0
    assert main(["remote", "default"]) == 0
    out, _ = capsys.readouterr()
    assert out == "foo\n"


class TestRemoteShouldHandleUppercaseRemoteName(TestDvc):
    upper_case_remote_name = "UPPERCASEREMOTE"

    def test(self):
        remote_url = Local.get_storagepath()
        ret = main(["remote", "add", self.upper_case_remote_name, remote_url])
        self.assertEqual(ret, 0)

        ret = main(["add", self.FOO])
        self.assertEqual(ret, 0)

        ret = main(["push", "-r", self.upper_case_remote_name])
        self.assertEqual(ret, 0)


def test_dir_checksum_should_be_key_order_agnostic(tmp_dir, dvc):
    tmp_dir.gen({"data": {"1": "1 content", "2": "2 content"}})

    path_info = PathInfo("data")
    with dvc.state:
        with patch.object(
            BaseRemote,
            "_collect_dir",
            return_value=[
                {"relpath": "1", "md5": "1"},
                {"relpath": "2", "md5": "2"},
            ],
        ):
            checksum1 = dvc.cache.local.get_dir_checksum(path_info)

        with patch.object(
            BaseRemote,
            "_collect_dir",
            return_value=[
                {"md5": "1", "relpath": "1"},
                {"md5": "2", "relpath": "2"},
            ],
        ):
            checksum2 = dvc.cache.local.get_dir_checksum(path_info)

    assert checksum1 == checksum2


def test_partial_push_n_pull(tmp_dir, dvc, tmp_path_factory):
    url = fspath(tmp_path_factory.mktemp("upstream"))
    dvc.config["remote"]["upstream"] = {"url": url}
    dvc.config["core"]["remote"] = "upstream"

    foo = tmp_dir.dvc_gen({"foo": "foo content"})[0].outs[0]
    bar = tmp_dir.dvc_gen({"bar": "bar content"})[0].outs[0]
    baz = tmp_dir.dvc_gen({"baz": {"foo": "baz content"}})[0].outs[0]

    # Faulty upload version, failing on foo
    original = LocalRemote._upload

    def unreliable_upload(self, from_file, to_info, name=None, **kwargs):
        if "foo" in name:
            raise Exception("stop foo")
        return original(self, from_file, to_info, name, **kwargs)

    with patch.object(LocalRemote, "_upload", unreliable_upload):
        with pytest.raises(UploadError) as upload_error_info:
            dvc.push()
        assert upload_error_info.value.amount == 3

        remote = dvc.cloud.get_remote("upstream")
        assert not remote.exists(remote.checksum_to_path_info(foo.checksum))
        assert remote.exists(remote.checksum_to_path_info(bar.checksum))
        assert not remote.exists(remote.checksum_to_path_info(baz.checksum))

    # Push everything and delete local cache
    dvc.push()
    remove(dvc.cache.local.cache_dir)

    with patch.object(LocalRemote, "_download", side_effect=Exception):
        with pytest.raises(DownloadError) as download_error_info:
            dvc.pull()
        # error count should be len(.dir + standalone file checksums)
        # since files inside dir are ignored if dir cache entry is missing
        assert download_error_info.value.amount == 3


def test_raise_on_too_many_open_files(tmp_dir, dvc, tmp_path_factory, mocker):
    storage = fspath(tmp_path_factory.mktemp("test_remote_base"))
    dvc.config["remote"]["local_remote"] = {"url": storage}
    dvc.config["core"]["remote"] = "local_remote"

    tmp_dir.dvc_gen({"file": "file content"})

    mocker.patch.object(
        LocalRemote,
        "_upload",
        side_effect=OSError(errno.EMFILE, "Too many open files"),
    )

    with pytest.raises(OSError) as e:
        dvc.push()
        assert e.errno == errno.EMFILE


def test_modify_missing_remote(tmp_dir, dvc):
    assert main(["remote", "modify", "myremote", "user", "xxx"]) == 251


def test_external_dir_resource_on_no_cache(tmp_dir, dvc, tmp_path_factory):
    # https://github.com/iterative/dvc/issues/2647, is some situations
    # (external dir dependency) cache is required to calculate dir md5
    external_dir = tmp_path_factory.mktemp("external_dir")
    (external_dir / "file").write_text("content")

    dvc.cache.local = None
    with pytest.raises(RemoteCacheRequiredError):
        dvc.run(deps=[fspath(external_dir)])


def test_push_order(tmp_dir, dvc, tmp_path_factory, mocker):
    url = fspath(tmp_path_factory.mktemp("upstream"))
    dvc.config["remote"]["upstream"] = {"url": url}
    dvc.config["core"]["remote"] = "upstream"

    tmp_dir.dvc_gen({"foo": {"bar": "bar content"}})
    tmp_dir.dvc_gen({"baz": "baz content"})

    mocked_upload = mocker.patch.object(LocalRemote, "_upload", return_value=0)
    dvc.push()
    # last uploaded file should be dir checksum
    assert mocked_upload.call_args[0][0].endswith(".dir")


def test_remote_modify_validation(dvc):
    remote_name = "drive"
    unsupported_config = "unsupported_config"
    assert (
        main(["remote", "add", "-d", remote_name, "gdrive://test/test"]) == 0
    )
    assert (
        main(
            ["remote", "modify", remote_name, unsupported_config, "something"]
        )
        == 251
    )
    config = configobj.ConfigObj(dvc.config.files["repo"])
    assert unsupported_config not in config['remote "{}"'.format(remote_name)]
