/**
 * Local-checkout entry for dsh-lmfdb-tools.
 *
 * The implementation lives at the repository root (`../../index.js`), so the
 * exact same file is the package entry when this repo is installed from a git
 * URL 鈥?there the package root is the bundle. This shim exists only so that
 * `install_bundle target=<repo>/dsh/lmfdb-tools` keeps working unchanged.
 */
export { name, inject, apply } from '../../index.js';
