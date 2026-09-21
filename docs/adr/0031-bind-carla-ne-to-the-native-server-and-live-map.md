# ADR 0031: Bind CARLA NE to the native server and live map

## Decision

CARLA runtime configuration binds the executable
`CarlaUE4/Binaries/Linux/CarlaUE4-Linux-Shipping` ELF inside the frozen server
distribution tree. The shell launcher is not a server identity.

Before NE, the external benchmark harness resolves the unique local RPC
`LISTEN` socket through `/proc` and binds it to one PID, process start time,
socket inode, and exact `/proc/<pid>/exe`. It repeats the check after NE and
stores both observations in a hash-bound `ne.server_session` capture. A missing,
ambiguous, mismatched, or changed server process fails NE only; it does not
invalidate an already independent Compile or SV result.

Compile reports the staged `.xodr` as `static_opendrive`, because this proves
which static network Scenic compiled. Successful NE separately records CARLA
client/server versions, the live `world.get_map().name`, and the SHA-256/byte
count of `world.get_map().to_opendrive()`. The live Town and OpenDRIVE bytes must
strictly match the declared Town and staged `.xodr`.

The source and staged Scenic artifact descriptors are part of
`carla_map_context`, alongside source and staged map descriptors. Hashes, byte
counts, attempt-local paths, and independent inodes are revalidated from the
terminal record.

## Consequences

- A successful NE cannot be attributed to an unrelated process listening on the
  configured port.
- Static Scenic map evidence is not mislabeled as live simulator evidence.
- Runtime layout reconstruction remains external to ChatScene and changes no
  generated Scenic or map bytes.
- The current host has no admissible CARLA listener evidence, so this decision
  does not create or imply a CARLA NE success claim.
