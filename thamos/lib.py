#!/usr/bin/env python3
# thamos
# Copyright(C) 2018, 2019 Fridolin Pokorny
#
# This program is free software: you can redistribute it and / or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

"""Core parts of library for interacting with Thoth."""

import os
import logging
import typing
from time import sleep
from time import monotonic
from contextlib import contextmanager
from functools import partial
from functools import wraps
import json
import urllib3

from yaspin import yaspin
from yaspin.spinners import Spinners

from .swagger_client.rest import ApiException
from .swagger_client import ApiClient
from .swagger_client import Configuration
from .swagger_client import PythonStack
from .swagger_client import RuntimeEnvironment
from .swagger_client import AdviseInput
from .swagger_client import AdviseApi
from .swagger_client import ImageAnalysisApi
from .swagger_client import ProvenanceApi
from .config import config as thoth_config
from .exceptions import UnknownAnalysisType

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


_LOGGER = logging.getLogger(__name__)


def with_api_client(func: typing.Callable):
    """Load configuration entries from Thoth configuration file."""

    @wraps(func)
    def wrapper(*args, **kwargs):
        config = Configuration()
        host = thoth_config.explicit_host
        if not host:
            thoth_config.load_config()
            host = thoth_config.content.get("host") or config.host

        thoth_config.api_discovery(host)

        _LOGGER.debug("Using API: %s", thoth_config.api_url)
        config.host = thoth_config.api_url
        config.verify_ssl = thoth_config.tls_verify

        start = monotonic()
        result = func(ApiClient(configuration=config), *args, **kwargs)
        _LOGGER.debug("Elapsed seconds processing request: %f", monotonic() - start)
        return result

    return wrapper


def _wait_for_analysis(status_func: callable, analysis_id: str) -> None:
    """Wait for ongoing analysis to finish."""

    @contextmanager
    def _no_spinner():
        yield

    spinner = partial(
        yaspin,
        Spinners.clock,
        text=f"Waiting for response from Thoth (analysis: {analysis_id})...",
    )
    if _LOGGER.getEffectiveLevel() == logging.DEBUG or bool(
        int(os.getenv("THAMOS_NO_PROGRESSBAR", 1))
    ):
        # CLI automatically injects THAMOS_NO_PROGRESSBAR=0 if user did not turned it off explictily.
        spinner = _no_spinner

    sleep_time = 0.5
    with spinner():
        while True:
            response = status_func(analysis_id)
            if response.status.finished_at is not None:
                break
            _LOGGER.debug(
                "Waiting for %r to finish for %s seconds (state: %s)",
                analysis_id,
                sleep_time,
                response.status.state,
            )

            sleep(sleep_time)
            sleep_time = min(sleep_time * 2, 10)


def _retrieve_analysis_result(
    retrieve_func: callable, analysis_id: str
) -> typing.Optional[dict]:
    """Retrieve analysis result, raise error if analysis failed."""
    try:
        return retrieve_func(analysis_id)
    except ApiException as exc:
        _LOGGER.debug(
            "Retrieved error response %s from server: %s", exc.status, exc.reason
        )
        response = json.loads(exc.body)
        _LOGGER.debug("Error from server: %s", response)
        _LOGGER.error("%s (analysis: %s)", response["error"], analysis_id)
        return None


@with_api_client
def advise(
    api_client: ApiClient,
    pipfile: str,
    pipfile_lock: str,
    recommendation_type: str = None,
    *,
    runtime_environment: dict = None,
    runtime_environment_name: str = None,
    nowait: bool = False,
    force: bool = False,
    limit: int = None,
    count: int = 1,
    debug: bool = False,
) -> typing.Optional[tuple]:
    """Submit a stack for adviser checks and wait for results."""
    if not pipfile:
        raise ValueError("No Pipfile content provided for advises")

    if runtime_environment and runtime_environment_name:
        raise ValueError(
            "Cannot use runtime_environment and runtime_environment_name at the same time"
        )

    # We use the explicit one if provided at the end.
    recommendation_type_explicit = recommendation_type
    recommendation_type = thoth_config.content.get("recommendation_type") or "stable"
    runtime_environment = runtime_environment or thoth_config.get_runtime_environment(
        runtime_environment_name
    )

    stack = PythonStack(requirements=pipfile, requirements_lock=pipfile_lock or "")

    if runtime_environment:
        # Override recommendation type specified explicitly in the runtime environment entry.
        if "recommendation_type" in runtime_environment:
            recommendation_type = runtime_environment.pop("recommendation_type")

        runtime_environment = RuntimeEnvironment(**runtime_environment)

    advise_input = AdviseInput(
        application_stack=stack, runtime_environment=runtime_environment
    )
    api_instance = AdviseApi(api_client)

    # Force to use the explicit one if user asked so.
    recommendation_type = recommendation_type_explicit or recommendation_type
    if recommendation_type:
        recommendation_type = recommendation_type.lower()

    parameters = {
        "recommendation_type": recommendation_type,
        "debug": debug,
        "force": force,
    }

    if limit is not None:
        parameters["limit"] = limit

    if count is not None:
        parameters["count"] = count

    response = api_instance.post_advise_python(advise_input, **parameters)

    _LOGGER.info("Successfully submitted advise analysis %r", response.analysis_id)
    if nowait:
        return response.analysis_id

    _wait_for_analysis(api_instance.get_advise_python_status, response.analysis_id)
    _LOGGER.debug("Retrieving adviser result for %r", response.analysis_id)
    response = _retrieve_analysis_result(
        api_instance.get_advise_python, response.analysis_id
    )
    if not response:
        return None

    _LOGGER.debug("Adviser check metadata: %r", response.metadata)

    return response.result["report"], response.result["error"]


@with_api_client
def provenance_check(
    api_client: ApiClient,
    pipfile: str,
    pipfile_lock: str,
    *,
    nowait: bool = False,
    force: bool = False,
    debug: bool = False,
) -> typing.Optional[tuple]:
    """Submit a stack for provenance checks and wait for results."""
    if not pipfile:
        raise ValueError("No Pipfile content provided for provenance checks")

    stack = PythonStack(requirements=pipfile, requirements_lock=pipfile_lock)
    api_instance = ProvenanceApi(api_client)
    response = api_instance.post_provenance_python(stack, debug=debug, force=force)
    _LOGGER.info(
        "Successfully submitted provenance check analysis %r", response.analysis_id
    )
    if nowait:
        return response.analysis_id

    _wait_for_analysis(api_instance.get_provenance_python_status, response.analysis_id)
    _LOGGER.debug("Retrieving provenance check result for %r", response.analysis_id)
    response = _retrieve_analysis_result(
        api_instance.get_provenance_python, response.analysis_id
    )
    if not response:
        return None

    _LOGGER.debug("Provenance check metadata: %r", response.metadata)
    return response.result["report"], response.result["error"]


@with_api_client
def image_analysis(
    api_client: ApiClient,
    image: str,
    *,
    registry_user: str = None,
    registry_password: str = None,
    verify_tls: bool = True,
    nowait: bool = False,
    force: bool = False,
    debug: bool = False,
) -> typing.Union[typing.Dict, str]:
    """Submit an image for analysis to Thoth."""
    if not image:
        raise ValueError("No image provided")

    api_instance = ImageAnalysisApi(api_client)
    if registry_user or registry_password:
        # Swagger client handles None in a different way - we need to explicitly avoid passing
        # registry user and registry password if they are not set.
        response = api_instance.post_analyze(
            image=image,
            debug=debug,
            registry_user=registry_user,
            registry_password=registry_password,
            verify_tls=verify_tls,
            force=force,
        )
    else:
        response = api_instance.post_analyze(
            image=image, debug=debug, verify_tls=verify_tls, force=force
        )

    _LOGGER.info(
        "Successfully submitted provenance check analysis %r", response.analysis_id
    )
    if nowait:
        return response.analysis_id

    _wait_for_analysis(api_instance.get_analyze_status, response.analysis_id)
    _LOGGER.debug(
        "Retrieving image analysis result result for %r", response.analysis_id
    )
    response = _retrieve_analysis_result(api_instance.get_analyze, response.analysis_id)
    if not response:
        return None

    _LOGGER.debug("Image analysis metadata: %r", response.metadata)
    return response.result


@with_api_client
def get_log(api_client: ApiClient, analysis_id: str):
    """Get log of an analysis - the analysis type and endpoint are automatically derived from analysis id."""
    if analysis_id.startswith("package-extract-"):
        api_instance = ImageAnalysisApi(api_client)
        method = api_instance.get_analyze_log
    elif analysis_id.startswith("provenance-checker-"):
        api_instance = ProvenanceApi(api_client)
        method = api_instance.get_provenance_python_log
    elif analysis_id.startswith("adviser-"):
        api_instance = AdviseApi(api_client)
        method = api_instance.get_advise_python_log
    else:
        raise UnknownAnalysisType(
            "Cannot determine analysis type from identifier: %r", analysis_id
        )

    return method(analysis_id).log


@with_api_client
def get_status(api_client: ApiClient, analysis_id: str):
    """Get status of an analysis - the analysis type and endpoint are automatically derived from analysis id."""
    if analysis_id.startswith("package-extract-"):
        api_instance = ImageAnalysisApi(api_client)
        method = api_instance.get_analyze_status
    elif analysis_id.startswith("provenance-checker-"):
        api_instance = ProvenanceApi(api_client)
        method = api_instance.get_provenance_python_status
    elif analysis_id.startswith("adviser-"):
        api_instance = AdviseApi(api_client)
        method = api_instance.get_advise_python_status
    else:
        raise UnknownAnalysisType(
            "Cannot determine analysis type from identifier: %r", analysis_id
        )

    return method(analysis_id).status.to_dict()
