/**
 * Image layout contract shared by the packer and the worker host: the virtual
 * root, where the composed config and the manifest sit inside the image, and
 * the working directories every image carries empty. One definition, two
 * consumers — the packer writes this layout, the worker host mounts it.
 */

/** Default virtual root; the runtime mounts the image here unless told otherwise. */
export const DEFAULT_ROOT = '/dsh'

/**
 * Leaf name of the packed base image: one gzip member holding the ustar archive.
 * The app build writes it beside the page and the page's boot fetches it from
 * there, so the extension is part of what a deployment serves.
 */
export const IMAGE_FILE_NAME = 'vfs-image.tar.gz'

/** Image path the composed profile is written to; the runtime's Loader reads it. */
export const IMAGE_CONFIG_PATH = 'config/cordis.yml'

/** Image path of the manifest the runtime reads before it wraps a single module. */
export const IMAGE_MANIFEST_PATH = 'config/vfs-manifest.json'

/** Home directory under the root; the process shim's `DSH_HOME`/`HOME` default. */
export const IMAGE_HOME_DIRECTORY = 'home'

/** Working directories the host tree expects to exist, empty. */
export const IMAGE_EMPTY_DIRECTORIES: readonly string[] = ['home/', 'workspace/', 'tmp/']

/**
 * Top-level directories an overlay archive may populate. Runtime code,
 * configuration, and the lowering manifest remain owned by the base image.
 */
export const IMAGE_OVERLAY_DIRECTORIES: readonly string[] = ['home', 'workspace']

/**
 * Identity of the lowered code shape, recorded in the image manifest by the
 * packer and required by the worker host: an image lowered by an older transform
 * would otherwise run against newer wrapper semantics. Bump on any change to
 * emitted code or to {@link WRAPPER_PARAMS}.
 */
export const LOWERING_VERSION = 'dsh-worker-transform/1'

/**
 * Free variables a lowered body expects from its wrapper, in order.
 *
 * Part of the image layout rather than of the transform, because the loader
 * wraps bodies it never parses: the packer emits against these names and the
 * worker binds them, with no compiler in the worker bundle to agree with.
 */
export const WRAPPER_PARAMS = [
  'exports', 'require', 'module', '__filename', '__dirname', '__dsh$meta', '__als',
] as const
