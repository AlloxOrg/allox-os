// SPDX-License-Identifier: Apache-2.0
#ifndef ALLOX_PROCESS_H
#define ALLOX_PROCESS_H

#define TASK_COMM_LEN 16
#define FILENAME_LEN 192

enum event_type {
    EVENT_FORK = 1,
    EVENT_EXEC = 2,
    EVENT_EXIT = 3,
    EVENT_ROOT = 4,
};

struct process_event {
    __u64 cookie;
    __u64 kernel_time_ns;
    __u64 birth_ns;
    __u64 parent_birth_ns;
    __u32 type;
    __u32 pid;
    __u32 ppid;
    __u32 former_tid;
    __u32 birth_pid;
    __u32 parent_birth_pid;
    char comm[TASK_COMM_LEN];
    char filename[FILENAME_LEN];
};

struct process_domain {
    __u64 cookie;
    __u64 birth_ns;
    __u32 birth_pid;
    __u32 reserved;
};

struct session_domain {
    __u64 cookie;
    __u64 birth_ns;
    __u64 root_pid;
};

#endif
