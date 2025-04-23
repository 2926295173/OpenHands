import argparse
import hashlib
import os
import shutil
import string
import tempfile
from enum import Enum
from pathlib import Path
from typing import List

import docker
from dirhash import dirhash  # type: ignore
from jinja2 import Environment, FileSystemLoader

import openhands
from openhands import __version__ as oh_version
from openhands.core.exceptions import AgentRuntimeBuildError
from openhands.core.logger import openhands_logger as logger
from openhands.runtime.builder import DockerRuntimeBuilder, RuntimeBuilder


class BuildFromImageType(Enum):
    DEPS = 'deps'  # Two-stage build: Use a pre-built dependencies image and copy /openhands folder


def get_runtime_image_repo() -> str:
    return os.getenv('OH_RUNTIME_RUNTIME_IMAGE_REPO', 'ghcr.io/all-hands-ai/runtime')


def get_deps_image_name() -> str:
    """Get the name of the dependencies image.
    
    Returns:
        str: The name of the dependencies image
    """
    repo = get_runtime_image_repo()
    deps_tag = f'oh_deps_v{oh_version}'
    return f'{repo}:{deps_tag}'


def _generate_dockerfile(
    base_image: str,
    build_from: BuildFromImageType = BuildFromImageType.DEPS,
    extra_deps: str | None = None,
    deps_image: str | None = None,
) -> str:
    """Generate the Dockerfile content for the runtime image based on the base image.

    Parameters:
    - base_image (str): The base image provided for the runtime image
    - build_from (BuildFromImageType): The build method for the runtime image.
    - extra_deps (str): Extra dependencies to install
    - deps_image (str): The dependencies image to use (only for DEPS build method)

    Returns:
    - str: The resulting Dockerfile content
    """
    env = Environment(
        loader=FileSystemLoader(
            searchpath=os.path.join(os.path.dirname(__file__), 'runtime_templates')
        )
    )
    
    template = env.get_template('Dockerfile.runtime.j2')
    dockerfile_content = template.render(
        base_image=base_image,
        deps_image=deps_image or get_deps_image_name(),
        extra_deps=extra_deps if extra_deps is not None else '',
    )
    
    return dockerfile_content


def get_runtime_image_repo_and_tag(base_image: str) -> tuple[str, str]:
    """Retrieves the Docker repo and tag associated with the Docker image.

    Parameters:
    - base_image (str): The name of the base Docker image

    Returns:
    - tuple[str, str]: The Docker repo and tag of the Docker image
    """
    if get_runtime_image_repo() in base_image:
        logger.debug(
            f'The provided image [{base_image}] is already a valid runtime image.\n'
            f'Will try to reuse it as is.'
        )

        if ':' not in base_image:
            base_image = base_image + ':latest'
        repo, tag = base_image.split(':')
        return repo, tag
    else:
        if ':' not in base_image:
            base_image = base_image + ':latest'
        [repo, tag] = base_image.split(':')

        # Hash the repo if it's too long
        if len(repo) > 32:
            repo_hash = hashlib.md5(repo[:-24].encode()).hexdigest()[:8]
            repo = f'{repo_hash}_{repo[-24:]}'  # Use 8 char hash + last 24 chars
        else:
            repo = repo.replace('/', '_s_')

        new_tag = f'oh_v{oh_version}_image_{repo}_tag_{tag}'

        # if it's still too long, hash the entire image name
        if len(new_tag) > 128:
            new_tag = f'oh_v{oh_version}_image_{hashlib.md5(new_tag.encode()).hexdigest()[:64]}'
            logger.warning(
                f'The new tag [{new_tag}] is still too long, so we use an hash of the entire image name: {new_tag}'
            )

        return get_runtime_image_repo(), new_tag


def build_deps_image(
    runtime_builder: RuntimeBuilder,
    platform: str | None = None,
    extra_deps: str | None = None,
    build_folder: str | None = None,
    dry_run: bool = False,
    extra_build_args: List[str] | None = None,
) -> str:
    """Build the dependencies image containing all OpenHands dependencies.

    Parameters:
    - runtime_builder (RuntimeBuilder): The runtime builder to use
    - platform (str): The target platform for the build (e.g. linux/amd64, linux/arm64)
    - extra_deps (str): Extra dependencies to install
    - build_folder (str): The directory to use for the build. If not provided a temporary directory will be used
    - dry_run (bool): if True, it will only ready the build folder. It will not actually build the Docker image
    - extra_build_args (List[str]): Additional build arguments to pass to the builder

    Returns:
    - str: The name of the dependencies image
    """
    if build_folder is None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = build_deps_image_in_folder(
                runtime_builder=runtime_builder,
                build_folder=Path(temp_dir),
                extra_deps=extra_deps,
                dry_run=dry_run,
                platform=platform,
                extra_build_args=extra_build_args,
            )
            return result

    result = build_deps_image_in_folder(
        runtime_builder=runtime_builder,
        build_folder=Path(build_folder),
        extra_deps=extra_deps,
        dry_run=dry_run,
        platform=platform,
        extra_build_args=extra_build_args,
    )
    return result


def build_deps_image_in_folder(
    runtime_builder: RuntimeBuilder,
    build_folder: Path,
    extra_deps: str | None,
    dry_run: bool,
    platform: str | None = None,
    extra_build_args: List[str] | None = None,
) -> str:
    """Prepares the build folder and builds the dependencies image.

    Parameters:
    - runtime_builder (RuntimeBuilder): The runtime builder to use
    - build_folder (Path): The directory to use for the build
    - extra_deps (str): Extra dependencies to install
    - dry_run (bool): if True, it will only ready the build folder. It will not actually build the Docker image
    - platform (str): The target platform for the build (e.g. linux/amd64, linux/arm64)
    - extra_build_args (List[str]): Additional build arguments to pass to the builder

    Returns:
    - str: The name of the dependencies image
    """
    deps_image_name = get_deps_image_name()
    logger.info(f'Building dependencies image: {deps_image_name}')

    # Create a Dockerfile for the dependencies image
    env = Environment(
        loader=FileSystemLoader(
            searchpath=os.path.join(os.path.dirname(__file__), 'runtime_templates')
        )
    )
    template = env.get_template('Dockerfile.deps.j2')
    dockerfile_content = template.render(
        extra_deps=extra_deps if extra_deps is not None else '',
    )

    with open(Path(build_folder, 'Dockerfile'), 'w') as file:
        file.write(dockerfile_content)

    # Copy wrapper scripts
    wrappers_dir = os.path.join(os.path.dirname(__file__), 'wrappers')
    target_dir = os.path.join(build_folder, 'code', 'openhands', 'runtime', 'utils', 'wrappers')
    os.makedirs(target_dir, exist_ok=True)
    for file in os.listdir(wrappers_dir):
        shutil.copy(os.path.join(wrappers_dir, file), os.path.join(target_dir, file))

    # Copy project files
    # Copy the source code to directory. It will end up in build_folder/code
    openhands_source_dir = Path(openhands.__file__).parent
    project_root = openhands_source_dir.parent
    
    # Copy the 'openhands' directory (Source code)
    shutil.copytree(
        openhands_source_dir,
        Path(build_folder, 'code', 'openhands'),
        ignore=shutil.ignore_patterns(
            '.*/',
            '__pycache__/',
            '*.pyc',
            '*.md',
        ),
        dirs_exist_ok=True,
    )

    # Copy pyproject.toml and poetry.lock files
    for file in ['pyproject.toml', 'poetry.lock']:
        src = Path(openhands_source_dir, file)
        if not src.exists():
            src = Path(project_root, file)
        if src.exists():
            shutil.copy2(src, Path(build_folder, 'code', file))

    if not dry_run:
        # Build the dependencies image
        runtime_builder.build_image(
            path=str(build_folder),
            tag=deps_image_name,
            platform=platform,
            extra_build_args=extra_build_args,
        )

    return deps_image_name


def build_runtime_image(
    base_image: str,
    runtime_builder: RuntimeBuilder,
    platform: str | None = None,
    extra_deps: str | None = None,
    build_folder: str | None = None,
    dry_run: bool = False,
    force_rebuild: bool = False,
    extra_build_args: List[str] | None = None,
    deps_image: str | None = None,
) -> str:
    """Prepares the final docker build folder.

    If dry_run is False, it will also build the OpenHands runtime Docker image using the docker build folder.

    Parameters:
    - base_image (str): The name of the base Docker image to use
    - runtime_builder (RuntimeBuilder): The runtime builder to use
    - platform (str): The target platform for the build (e.g. linux/amd64, linux/arm64)
    - extra_deps (str): Extra dependencies to install
    - build_folder (str): The directory to use for the build. If not provided a temporary directory will be used
    - dry_run (bool): if True, it will only ready the build folder. It will not actually build the Docker image
    - force_rebuild (bool): if True, it will force rebuilding even if the image already exists
    - extra_build_args (List[str]): Additional build arguments to pass to the builder
    - deps_image (str): The dependencies image to use (if None, will use the default)

    Returns:
    - str: <image_repo>:<MD5 hash>. Where MD5 hash is the hash of the docker build folder

    See https://docs.all-hands.dev/modules/usage/architecture/runtime_build for more details.
    """
    # If using the dependencies image approach, first ensure the dependencies image exists
    if deps_image is None:
        deps_image = get_deps_image_name()
        
    # Check if the dependencies image exists
    try:
        runtime_builder.get_image(deps_image)
        logger.info(f'Using existing dependencies image: {deps_image}')
    except Exception:
        # Dependencies image doesn't exist, build it
        logger.info(f'Dependencies image {deps_image} not found. Building it...')
        deps_image = build_deps_image(
            runtime_builder=runtime_builder,
            platform=platform,
            extra_deps=extra_deps,
            build_folder=build_folder,
            dry_run=dry_run,
            extra_build_args=extra_build_args,
        )

    if build_folder is None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result = build_runtime_image_from_deps(
                base_image=base_image,
                runtime_builder=runtime_builder,
                deps_image=deps_image,
                build_folder=Path(temp_dir),
                extra_deps=extra_deps,
                dry_run=dry_run,
                platform=platform,
                extra_build_args=extra_build_args,
                force_rebuild=force_rebuild,
            )
            return result

    result = build_runtime_image_from_deps(
        base_image=base_image,
        runtime_builder=runtime_builder,
        deps_image=deps_image,
        build_folder=Path(build_folder),
        extra_deps=extra_deps,
        dry_run=dry_run,
        platform=platform,
        extra_build_args=extra_build_args,
        force_rebuild=force_rebuild,
    )
    return result


def prep_build_folder(
    build_folder: Path,
    base_image: str,
    build_from: BuildFromImageType,
    extra_deps: str | None,
    deps_image: str | None = None,
) -> None:
    """Prepare the build folder with necessary files.
    
    Parameters:
    - build_folder (Path): The directory to use for the build
    - base_image (str): The base image to use
    - build_from (BuildFromImageType): The build method to use
    - extra_deps (str): Extra dependencies to install
    - deps_image (str): The dependencies image to use (only for DEPS build method)
    """
    # Copy the source code to directory. It will end up in build_folder/code
    openhands_source_dir = Path(openhands.__file__).parent
    project_root = openhands_source_dir.parent
    logger.debug(f'Building source distribution using project root: {project_root}')

    # For DEPS build method, we only need to copy the wrapper scripts
    if build_from == BuildFromImageType.DEPS:
        # Copy the 'openhands' directory (Source code)
        os.makedirs(os.path.join(build_folder, 'code', 'openhands'), exist_ok=True)
        shutil.copytree(
            openhands_source_dir,
            Path(build_folder, 'code', 'openhands'),
            ignore=shutil.ignore_patterns(
                '.*/',
                '__pycache__/',
                '*.pyc',
                '*.md',
            ),
            dirs_exist_ok=True,
        )
    else:
        # Copy the 'openhands' directory (Source code)
        shutil.copytree(
            openhands_source_dir,
            Path(build_folder, 'code', 'openhands'),
            ignore=shutil.ignore_patterns(
                '.*/',
                '__pycache__/',
                '*.pyc',
                '*.md',
            ),
        )

        # Copy pyproject.toml and poetry.lock files
        for file in ['pyproject.toml', 'poetry.lock']:
            src = Path(openhands_source_dir, file)
            if not src.exists():
                src = Path(project_root, file)
            shutil.copy2(src, Path(build_folder, 'code', file))

    # Create a Dockerfile and write it to build_folder
    dockerfile_content = _generate_dockerfile(
        base_image,
        build_from=build_from,
        extra_deps=extra_deps,
        deps_image=deps_image,
    )
    with open(Path(build_folder, 'Dockerfile'), 'w') as file:
        file.write(dockerfile_content)


def get_hash_for_source_files() -> str:
    """Get a hash of the source files.

    Returns:
    - str: The hash of the source files
    """
    openhands_source_dir = Path(openhands.__file__).parent
    source_hash = dirhash(
        openhands_source_dir,
        'md5',
        ignore_hidden=True,
        excluded_extensions=['.pyc', '.md'],
    )
    return source_hash[:8]


def build_runtime_image_from_deps(
    base_image: str,
    runtime_builder: RuntimeBuilder,
    deps_image: str,
    build_folder: Path,
    extra_deps: str | None = None,
    dry_run: bool = False,
    platform: str | None = None,
    extra_build_args: List[str] | None = None,
    force_rebuild: bool = False,
) -> str:
    """Build a runtime image using the dependencies image.

    Parameters:
    - base_image (str): The base image to use
    - runtime_builder (RuntimeBuilder): The runtime builder to use
    - deps_image (str): The dependencies image to use
    - build_folder (Path): The directory to use for the build
    - extra_deps (str): Extra dependencies to install
    - dry_run (bool): if True, it will only ready the build folder. It will not actually build the Docker image
    - platform (str): The target platform for the build (e.g. linux/amd64, linux/arm64)
    - extra_build_args (List[str]): Additional build arguments to pass to the builder
    - force_rebuild (bool): if True, it will force rebuilding even if the image already exists

    Returns:
    - str: The name of the runtime image
    """
    runtime_image_repo, runtime_image_tag = get_runtime_image_repo_and_tag(base_image)
    source_tag = f'{runtime_image_tag}_{get_hash_for_source_files()}'
    runtime_image_name = f'{runtime_image_repo}:{source_tag}'

    logger.info(f'Building runtime image: {runtime_image_name}')

    # Check if the image already exists
    if not force_rebuild:
        try:
            runtime_builder.get_image(runtime_image_name)
            logger.info(f'Runtime image {runtime_image_name} already exists. Reusing it.')
            return runtime_image_name
        except Exception:
            logger.info(f'Runtime image {runtime_image_name} not found. Building it...')

    # Create a Dockerfile for the runtime image
    dockerfile_content = _generate_dockerfile(
        base_image=base_image,
        deps_image=deps_image,
        extra_deps=extra_deps,
    )

    with open(Path(build_folder, 'Dockerfile'), 'w') as file:
        file.write(dockerfile_content)

    if not dry_run:
        # Build the runtime image
        runtime_builder.build_image(
            path=str(build_folder),
            tag=runtime_image_name,
            platform=platform,
            extra_build_args=extra_build_args,
        )

    return runtime_image_name


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--base_image', type=str, default='nikolaik/python-nodejs:python3.12-nodejs22'
    )
    parser.add_argument('--build_folder', type=str, default=None)
    parser.add_argument('--force_rebuild', action='store_true', default=False)
    parser.add_argument('--platform', type=str, default=None)
    parser.add_argument('--deps_image', type=str, default=None,
                        help='The dependencies image to use')
    parser.add_argument('--build_deps_only', action='store_true', default=False,
                        help='Only build the dependencies image')
    args = parser.parse_args()

    # If only building the dependencies image
    if args.build_deps_only:
        deps_image = build_deps_image(
            runtime_builder=DockerRuntimeBuilder(docker.from_env()),
            build_folder=args.build_folder,
            platform=args.platform,
        )
        logger.info(f'Dependencies image built: {deps_image}')
        exit(0)

    if args.build_folder is not None:
        # If a build_folder is provided, we do not actually build the Docker image. We copy the necessary source code
        # and create a Dockerfile dynamically and place it in the build_folder only. This allows the Docker image to
        # then be created using the Dockerfile (most likely using the containers/build.sh script)
        build_folder = args.build_folder
        assert os.path.exists(
            build_folder
        ), f'Build folder {build_folder} does not exist'
        logger.debug(
            f'Copying the source code and generating the Dockerfile in the build folder: {build_folder}'
        )

        runtime_image_repo, runtime_image_tag = get_runtime_image_repo_and_tag(
            args.base_image
        )
        logger.debug(
            f'Runtime image repo: {runtime_image_repo} and runtime image tag: {runtime_image_tag}'
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            # dry_run is true so we only prepare a temp_dir containing the required source code and the Dockerfile. We
            # then obtain the MD5 hash of the folder and return <image_repo>:<temp_dir_md5_hash>
            runtime_image_hash_name = build_runtime_image(
                args.base_image,
                runtime_builder=DockerRuntimeBuilder(docker.from_env()),
                build_folder=temp_dir,
                dry_run=True,
                force_rebuild=args.force_rebuild,
                platform=args.platform,
                deps_image=args.deps_image,
            )

            _runtime_image_repo, runtime_image_source_tag = (
                runtime_image_hash_name.split(':')
            )

            # Move contents of temp_dir to build_folder
            shutil.copytree(temp_dir, build_folder, dirs_exist_ok=True)
        logger.debug(
            f'Build folder [{build_folder}] is ready: {os.listdir(build_folder)}'
        )

        # We now update the config.sh in the build_folder to contain the required values. This is used in the
        # containers/build.sh script which is called to actually build the Docker image
        with open(os.path.join(build_folder, 'config.sh'), 'a') as file:
            file.write(
                (
                    f'\n'
                    f'DOCKER_IMAGE_TAG={runtime_image_tag}\n'
                    f'DOCKER_IMAGE_SOURCE_TAG={runtime_image_source_tag}\n'
                )
            )

        logger.debug(
            f'`config.sh` is updated with the image repo[{runtime_image_repo}] and tags [{runtime_image_tag}, {runtime_image_source_tag}]'
        )
        logger.debug(
            f'Dockerfile, source code and config.sh are ready in {build_folder}'
        )
    else:
        # If a build_folder is not provided, after copying the required source code and dynamically creating the
        # Dockerfile, we actually build the Docker image
        logger.debug('Building image in a temporary folder')
        docker_builder = DockerRuntimeBuilder(docker.from_env())
        image_name = build_runtime_image(
            args.base_image,
            docker_builder,
            platform=args.platform,
            force_rebuild=args.force_rebuild,
            deps_image=args.deps_image,
        )
        logger.debug(f'\nBuilt image: {image_name}\n')