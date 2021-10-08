# Copyright 2020 The StackStorm Authors.
# Copyright 2019 Extreme Networks, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import os.path
import stat
import errno

import six
from mongoengine import ValidationError

# TODO: Encapsulate mongoengine errors in our persistence layer. Exceptions
#       that bubble up to this layer should be core Python exceptions or
#       StackStorm defined exceptions.

from st2api.controllers import resource
from st2api.controllers.v1.action_views import ActionViewsController
from st2common import log as logging
from st2common.constants.triggers import ACTION_FILE_WRITTEN_TRIGGER
from st2common.exceptions.action import InvalidActionParameterException
from st2common.exceptions.apivalidation import ValueValidationException
from st2common.exceptions.rbac import ResourceAccessDeniedError
from st2common.persistence.action import Action
from st2common.models.api.action import ActionAPI
from st2common.persistence.pack import Pack
from st2common.rbac.types import PermissionType
from st2common.rbac.backends import get_rbac_backend
from st2common.router import abort
from st2common.router import GenericRequestParam
from st2common.router import Response
from st2common.validators.api.misc import validate_not_part_of_system_pack
from st2common.validators.api.misc import validate_not_part_of_system_pack_by_name
from st2common.content.utils import get_pack_base_path
from st2common.content.utils import get_pack_resource_file_abs_path
from st2common.content.utils import get_relative_path_to_pack_file
from st2common.services.packs import delete_action_files_from_pack
from st2common.services.packs import clone_action_files
from st2common.services.packs import clone_action_db
from st2common.services.packs import temp_backup_action_files
from st2common.services.packs import remove_temp_action_files
from st2common.services.packs import restore_temp_action_files
from st2common.transport.reactor import TriggerDispatcher
from st2common.util.system_info import get_host_info
import st2common.validators.api.action as action_validator

http_client = six.moves.http_client

LOG = logging.getLogger(__name__)


class ActionsController(resource.ContentPackResourceController):
    """
    Implements the RESTful web endpoint that handles
    the lifecycle of Actions in the system.
    """

    views = ActionViewsController()

    model = ActionAPI
    access = Action
    supported_filters = {"name": "name", "pack": "pack", "tags": "tags.name"}

    query_options = {"sort": ["pack", "name"]}

    valid_exclude_attributes = ["parameters", "notify"]

    def __init__(self, *args, **kwargs):
        super(ActionsController, self).__init__(*args, **kwargs)
        self._trigger_dispatcher = TriggerDispatcher(LOG)

    def get_all(
        self,
        exclude_attributes=None,
        include_attributes=None,
        sort=None,
        offset=0,
        limit=None,
        requester_user=None,
        **raw_filters,
    ):
        return super(ActionsController, self)._get_all(
            exclude_fields=exclude_attributes,
            include_fields=include_attributes,
            sort=sort,
            offset=offset,
            limit=limit,
            raw_filters=raw_filters,
            requester_user=requester_user,
        )

    def get_one(self, ref_or_id, requester_user):
        return super(ActionsController, self)._get_one(
            ref_or_id,
            requester_user=requester_user,
            permission_type=PermissionType.ACTION_VIEW,
        )

    def post(self, action, requester_user):
        """
        Create a new action.

        Handles requests:
            POST /actions/
        """

        permission_type = PermissionType.ACTION_CREATE
        rbac_utils = get_rbac_backend().get_utils_class()
        rbac_utils.assert_user_has_resource_api_permission(
            user_db=requester_user, resource_api=action, permission_type=permission_type
        )

        try:
            # Perform validation
            validate_not_part_of_system_pack(action)
            action_validator.validate_action(action)
        except (
            ValidationError,
            ValueError,
            ValueValidationException,
            InvalidActionParameterException,
        ) as e:
            LOG.exception("Unable to create action data=%s", action)
            abort(http_client.BAD_REQUEST, six.text_type(e))
            return

        # Write pack data files to disk (if any are provided)
        data_files = getattr(action, "data_files", [])
        written_data_files = []
        if data_files:
            written_data_files = self._handle_data_files(
                pack_ref=action.pack, data_files=data_files
            )

        action_model = ActionAPI.to_model(action)

        LOG.debug("/actions/ POST verified ActionAPI object=%s", action)
        action_db = Action.add_or_update(action_model)
        LOG.debug("/actions/ POST saved ActionDB object=%s", action_db)

        # Dispatch an internal trigger for each written data file. This way user
        # automate comitting this files to git using StackStorm rule
        if written_data_files:
            self._dispatch_trigger_for_written_data_files(
                action_db=action_db, written_data_files=written_data_files
            )

        extra = {"acion_db": action_db}
        LOG.audit("Action created. Action.id=%s" % (action_db.id), extra=extra)
        action_api = ActionAPI.from_model(action_db)

        return Response(json=action_api, status=http_client.CREATED)

    def put(self, action, ref_or_id, requester_user):
        action_db = self._get_by_ref_or_id(ref_or_id=ref_or_id)

        # Assert permissions
        permission_type = PermissionType.ACTION_MODIFY
        rbac_utils = get_rbac_backend().get_utils_class()
        rbac_utils.assert_user_has_resource_db_permission(
            user_db=requester_user,
            resource_db=action_db,
            permission_type=permission_type,
        )

        action_id = action_db.id

        if not getattr(action, "pack", None):
            action.pack = action_db.pack

        # Perform validation
        validate_not_part_of_system_pack(action)
        action_validator.validate_action(action)

        # Write pack data files to disk (if any are provided)
        data_files = getattr(action, "data_files", [])
        written_data_files = []
        if data_files:
            written_data_files = self._handle_data_files(
                pack_ref=action.pack, data_files=data_files
            )

        try:
            action_db = ActionAPI.to_model(action)
            LOG.debug("/actions/ PUT incoming action: %s", action_db)
            action_db.id = action_id
            action_db = Action.add_or_update(action_db)
            LOG.debug("/actions/ PUT after add_or_update: %s", action_db)
        except (ValidationError, ValueError) as e:
            LOG.exception("Unable to update action data=%s", action)
            abort(http_client.BAD_REQUEST, six.text_type(e))
            return

        # Dispatch an internal trigger for each written data file. This way user
        # automate committing this files to git using StackStorm rule
        if written_data_files:
            self._dispatch_trigger_for_written_data_files(
                action_db=action_db, written_data_files=written_data_files
            )

        action_api = ActionAPI.from_model(action_db)
        LOG.debug("PUT /actions/ client_result=%s", action_api)

        return action_api

    def delete(self, options, ref_or_id, requester_user):
        """
        Delete an action.

        Handles requests:
            POST /actions/1?_method=delete
            DELETE /actions/1
            DELETE /actions/mypack.myaction
        """
        action_db = self._get_by_ref_or_id(ref_or_id=ref_or_id)
        action_id = action_db.id

        permission_type = PermissionType.ACTION_DELETE
        rbac_utils = get_rbac_backend().get_utils_class()
        rbac_utils.assert_user_has_resource_db_permission(
            user_db=requester_user,
            resource_db=action_db,
            permission_type=permission_type,
        )

        try:
            validate_not_part_of_system_pack(action_db)
        except ValueValidationException as e:
            abort(http_client.BAD_REQUEST, six.text_type(e))

        LOG.debug(
            "DELETE /actions/ lookup with ref_or_id=%s found object: %s",
            ref_or_id,
            action_db,
        )

        pack_name = action_db["pack"]
        entry_point = action_db["entry_point"]
        metadata_file = action_db["metadata_file"]

        try:
            Action.delete(action_db)
        except Exception as e:
            LOG.error(
                'Database delete encountered exception during delete of id="%s". '
                "Exception was %s",
                action_id,
                e,
            )
            abort(http_client.INTERNAL_SERVER_ERROR, six.text_type(e))
            return

        if options.remove_files:
            try:
                delete_action_files_from_pack(
                    pack_name=pack_name,
                    entry_point=entry_point,
                    metadata_file=metadata_file,
                )
            except PermissionError as e:
                LOG.error("No permission to delete resource files from disk.")
                action_db.id = None
                Action.add_or_update(action_db)
                abort(http_client.FORBIDDEN, six.text_type(e))
                return
            except Exception as e:
                LOG.error(
                    "Exception encountered during deleting resource files from disk. "
                    "Exception was %s",
                    e,
                )
                action_db.id = None
                Action.add_or_update(action_db)
                abort(http_client.INTERNAL_SERVER_ERROR, six.text_type(e))
                return

        extra = {"action_db": action_db}
        LOG.audit("Action deleted. Action.id=%s" % (action_db.id), extra=extra)
        return Response(status=http_client.NO_CONTENT)

    def clone(self, dest_data, ref_or_id, requester_user):
        """
        Clone an action from source pack to destination pack.
        Handles requests:
            POST /actions/{ref_or_id}/clone
        """

        source_action_db = self._get_by_ref_or_id(ref_or_id=ref_or_id)
        if not source_action_db:
            msg = "The requested source for cloning operation doesn't exists"
            abort(http_client.BAD_REQUEST, six.text_type(msg))

        extra = {"action_db": source_action_db}
        LOG.audit(
            "Source action found. Action.id=%s" % (source_action_db.id), extra=extra
        )

        try:
            permission_type = PermissionType.ACTION_VIEW
            rbac_utils = get_rbac_backend().get_utils_class()
            rbac_utils.assert_user_has_resource_db_permission(
                user_db=requester_user,
                resource_db=source_action_db,
                permission_type=permission_type,
            )
        except ResourceAccessDeniedError as e:
            abort(http_client.UNAUTHORIZED, six.text_type(e))

        cloned_dest_action_db = clone_action_db(
            source_action_db=source_action_db,
            dest_pack=dest_data.dest_pack,
            dest_action=dest_data.dest_action,
        )

        cloned_action_api = ActionAPI.from_model(cloned_dest_action_db)

        try:
            permission_type = PermissionType.ACTION_CREATE
            rbac_utils.assert_user_has_resource_api_permission(
                user_db=requester_user,
                resource_api=cloned_action_api,
                permission_type=permission_type,
            )
        except ResourceAccessDeniedError as e:
            abort(http_client.UNAUTHORIZED, six.text_type(e))

        dest_pack_base_path = get_pack_base_path(pack_name=dest_data.dest_pack)

        if not os.path.isdir(dest_pack_base_path):
            msg = "Destination pack '%s' doesn't exist" % (dest_data.dest_pack)
            abort(http_client.BAD_REQUEST, six.text_type(msg))

        dest_pack_base_path = get_pack_base_path(pack_name=dest_data.dest_pack)
        dest_ref = ".".join([dest_data.dest_pack, dest_data.dest_action])
        dest_action_db = self._get_by_ref(resource_ref=dest_ref)

        try:
            validate_not_part_of_system_pack_by_name(dest_data.dest_pack)
        except ValueValidationException as e:
            abort(http_client.BAD_REQUEST, six.text_type(e))

        if dest_action_db:
            if not dest_data.overwrite:
                msg = "The requested destination action already exists"
                abort(http_client.BAD_REQUEST, six.text_type(msg))

            try:
                permission_type = PermissionType.ACTION_DELETE
                rbac_utils.assert_user_has_resource_db_permission(
                    user_db=requester_user,
                    resource_db=dest_action_db,
                    permission_type=permission_type,
                )
                options = GenericRequestParam(remove_files=True)
                dest_metadata_file = dest_action_db["metadata_file"]
                dest_entry_point = dest_action_db["entry_point"]
                temp_backup_action_files(
                    dest_pack_base_path, dest_metadata_file, dest_entry_point
                )
                self.delete(options, dest_ref, requester_user)
            except ResourceAccessDeniedError as e:
                abort(http_client.UNAUTHORIZED, six.text_type(e))
            except Exception as e:
                LOG.debug(
                    "Exception encountered during deleting existing destination action. "
                    "Exception was: %s",
                    e,
                )
                abort(http_client.INTERNAL_SERVER_ERROR, six.text_type(e))

        try:
            clone_action_files(
                source_action_db=source_action_db,
                dest_action_db=cloned_dest_action_db,
                dest_pack_base_path=dest_pack_base_path,
            )

            post_response = self.post(cloned_action_api, requester_user)
            if post_response.status_code != http_client.CREATED:
                raise Exception("Could not add cloned action to database.")

            extra = {"cloned_acion_db": cloned_dest_action_db}
            LOG.audit(
                "Action cloned. Action.id=%s" % (cloned_dest_action_db.id), extra=extra
            )
            if dest_action_db:
                remove_temp_action_files(dest_pack_base_path)
            return post_response
        except PermissionError as e:
            LOG.error("No permission to clone the action. Exception was %s", e)
            if dest_action_db:
                restore_temp_action_files(
                    dest_pack_base_path, dest_metadata_file, dest_entry_point
                )
                dest_action_db.id = None
                Action.add_or_update(dest_action_db)
                remove_temp_action_files(dest_pack_base_path)
            abort(http_client.FORBIDDEN, six.text_type(e))
        except Exception as e:
            LOG.error(
                "Exception encountered during cloning action. Exception was %s",
                e,
            )
            delete_action_files_from_pack(
                pack_name=cloned_dest_action_db["pack"],
                entry_point=cloned_dest_action_db["entry_point"],
                metadata_file=cloned_dest_action_db["metadata_file"],
            )
            if dest_action_db:
                restore_temp_action_files(
                    dest_pack_base_path, dest_metadata_file, dest_entry_point
                )
                dest_action_db.id = None
                Action.add_or_update(dest_action_db)
                remove_temp_action_files(dest_pack_base_path)
            abort(http_client.INTERNAL_SERVER_ERROR, six.text_type(e))

    def _handle_data_files(self, pack_ref, data_files):
        """
        Method for handling action data files.

        This method performs two tasks:

        1. Writes files to disk
        2. Updates affected PackDB model
        """
        # Write files to disk
        written_file_paths = self._write_data_files_to_disk(
            pack_ref=pack_ref, data_files=data_files
        )

        # Update affected PackDB model (update a list of files)
        # Update PackDB
        self._update_pack_model(
            pack_ref=pack_ref,
            data_files=data_files,
            written_file_paths=written_file_paths,
        )

        return written_file_paths

    def _write_data_files_to_disk(self, pack_ref, data_files):
        """
        Write files to disk.
        """
        written_file_paths = []

        for data_file in data_files:
            file_path = data_file["file_path"]
            content = data_file["content"]

            file_path = get_pack_resource_file_abs_path(
                pack_ref=pack_ref, resource_type="action", file_path=file_path
            )

            LOG.debug('Writing data file "%s" to "%s"' % (str(data_file), file_path))

            try:
                self._write_data_file(
                    pack_ref=pack_ref, file_path=file_path, content=content
                )
            except (OSError, IOError) as e:
                # Throw a more user-friendly exception on Permission denied error
                if e.errno == errno.EACCES:
                    msg = (
                        'Unable to write data to "%s" (permission denied). Make sure '
                        "permissions for that pack directory are configured correctly so "
                        "st2api can write to it." % (file_path)
                    )
                    raise ValueError(msg)
                raise e

            written_file_paths.append(file_path)

        return written_file_paths

    def _update_pack_model(self, pack_ref, data_files, written_file_paths):
        """
        Update PackDB models (update files list).
        """
        file_paths = []  # A list of paths relative to the pack directory for new files
        for file_path in written_file_paths:
            file_path = get_relative_path_to_pack_file(
                pack_ref=pack_ref, file_path=file_path
            )
            file_paths.append(file_path)

        pack_db = Pack.get_by_ref(pack_ref)
        pack_db.files = set(pack_db.files)
        pack_db.files.update(set(file_paths))
        pack_db.files = list(pack_db.files)
        pack_db = Pack.add_or_update(pack_db)

        return pack_db

    def _write_data_file(self, pack_ref, file_path, content):
        """
        Write data file on disk.
        """
        # Throw if pack directory doesn't exist
        pack_base_path = get_pack_base_path(pack_name=pack_ref)
        if not os.path.isdir(pack_base_path):
            raise ValueError('Directory for pack "%s" doesn\'t exist' % (pack_ref))

        # Create pack sub-directory tree if it doesn't exist
        directory = os.path.dirname(file_path)

        if not os.path.isdir(directory):
            # NOTE: We apply same permission bits as we do on pack install. If we don't do that,
            # st2api won't be able to write to pack sub-directory
            mode = stat.S_IRWXU | stat.S_IRWXG | stat.S_IROTH | stat.S_IXOTH
            os.makedirs(directory, mode)

        with open(file_path, "w") as fp:
            fp.write(content)

    def _dispatch_trigger_for_written_data_files(self, action_db, written_data_files):
        trigger = ACTION_FILE_WRITTEN_TRIGGER["name"]
        host_info = get_host_info()

        for file_path in written_data_files:
            payload = {
                "ref": action_db.ref,
                "file_path": file_path,
                "host_info": host_info,
            }
            self._trigger_dispatcher.dispatch(trigger=trigger, payload=payload)


actions_controller = ActionsController()
