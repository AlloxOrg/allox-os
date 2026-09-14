# allox-process-tree

Optional eBPF process-tree observer for Allox OS. The Python adapter is published through the
`allox.process_trackers` entry-point group; the native collector is built from `native/` and
installed separately in the trusted Guest image.

```sh
pip install allox-process-tree
make -C native
allox-process-tree-bootstrap
allox-workspace-daemon --process-tracking ebpf ...
```
