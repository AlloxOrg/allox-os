// SPDX-License-Identifier: GPL-2.0 OR BSD-3-Clause
/* Process-domain propagation follows ActPlane's fork/exec/exit design. */

#include <linux/bpf.h>
#include <bpf/bpf_helpers.h>
#include "allox_process.h"

struct trace_entry {
    __u16 type;
    __u8 flags;
    __u8 preempt_count;
    __s32 pid;
};

struct sched_process_fork_args {
    struct trace_entry common;
    char parent_comm[TASK_COMM_LEN];
    __s32 parent_pid;
    char child_comm[TASK_COMM_LEN];
    __s32 child_pid;
};

struct sched_process_exec_args {
    struct trace_entry common;
    __u32 data_loc_filename;
    __s32 pid;
    __s32 old_pid;
};

struct sched_process_template_args {
    struct trace_entry common;
    char comm[TASK_COMM_LEN];
    __s32 pid;
    __s32 prio;
};

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 65536);
    __type(key, __u32);
    __type(value, struct process_domain);
} process_domains SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 4096);
    __type(key, __u64);
    __type(value, struct session_domain);
} session_domains SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_RINGBUF);
    __uint(max_entries, 256 * 1024);
} events SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct process_event);
} scratch SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, __u64);
} lost SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 4096);
    __type(key, __u64);
    __type(value, __u64);
} produced SEC(".maps");

static __always_inline void lost_event(void)
{
    __u32 zero = 0;
    __u64 *count = bpf_map_lookup_elem(&lost, &zero);
    if (count)
        __sync_fetch_and_add(count, 1);
}

static __always_inline struct process_event *event_buffer(void)
{
    __u32 zero = 0;
    struct process_event *event = bpf_map_lookup_elem(&scratch, &zero);
    if (event)
        __builtin_memset(event, 0, sizeof(*event));
    return event;
}

static __always_inline void submit(struct process_event *event)
{
    if (!event || bpf_ringbuf_output(&events, event, sizeof(*event), 0))
        lost_event();
    else {
        __u64 *count = bpf_map_lookup_elem(&produced, &event->cookie);
        if (count)
            __sync_fetch_and_add(count, 1);
        else
            lost_event();
    }
}

static __always_inline struct process_domain *domain_for_current(__u32 pid)
{
    struct process_domain *domain = bpf_map_lookup_elem(&process_domains, &pid);
    if (domain)
        return domain;
    __u64 cgroup_id = bpf_get_current_cgroup_id();
    struct session_domain *session = bpf_map_lookup_elem(&session_domains, &cgroup_id);
    if (!session)
        return 0;
    __u64 zero = 0;
    if (__sync_val_compare_and_swap(&session->root_pid, zero, pid) == zero) {
        struct process_domain root = {
            .cookie = session->cookie, .birth_ns = session->birth_ns, .birth_pid = pid
        };
        if (bpf_map_update_elem(&process_domains, &pid, &root, BPF_ANY)) {
            lost_event();
            return 0;
        }
        struct process_event *event = event_buffer();
        if (!event) {
            lost_event();
            return 0;
        }
        event->cookie = root.cookie;
        event->birth_ns = root.birth_ns;
        event->birth_pid = root.birth_pid;
        event->kernel_time_ns = bpf_ktime_get_ns();
        event->type = EVENT_ROOT;
        event->pid = pid;
        bpf_get_current_comm(event->comm, sizeof(event->comm));
        submit(event);
    }
    return bpf_map_lookup_elem(&process_domains, &pid);
}

SEC("tp/sched/sched_process_fork")
int handle_fork(struct sched_process_fork_args *ctx)
{
    __u32 parent = ctx->parent_pid;
    __u32 child = ctx->child_pid;
    struct process_domain *domain = domain_for_current(parent);
    if (!domain)
        return 0;
    struct process_domain inherited = {
        .cookie = domain->cookie, .birth_ns = bpf_ktime_get_ns(), .birth_pid = child
    };
    __u64 parent_birth = domain->birth_ns;
    __u32 parent_birth_pid = domain->birth_pid;
    if (bpf_map_update_elem(&process_domains, &child, &inherited, BPF_ANY))
        lost_event();
    struct process_event *event = event_buffer();
    if (!event) {
        lost_event();
        return 0;
    }
    event->cookie = inherited.cookie;
    event->birth_ns = inherited.birth_ns;
    event->birth_pid = inherited.birth_pid;
    event->parent_birth_ns = parent_birth;
    event->parent_birth_pid = parent_birth_pid;
    event->kernel_time_ns = bpf_ktime_get_ns();
    event->type = EVENT_FORK;
    event->pid = child;
    event->ppid = parent;
    __builtin_memcpy(event->comm, ctx->child_comm, TASK_COMM_LEN);
    submit(event);
    return 0;
}

SEC("tp/sched/sched_process_exec")
int handle_exec(struct sched_process_exec_args *ctx)
{
    __u64 pid_tgid = bpf_get_current_pid_tgid();
    __u32 pid = pid_tgid >> 32;
    __u32 former_tid = ctx->old_pid;
    struct process_domain *domain = bpf_map_lookup_elem(&process_domains, &former_tid);
    if (!domain)
        domain = domain_for_current(pid);
    if (former_tid != pid) {
        if (domain) {
            struct process_domain moved = *domain;
            if (bpf_map_update_elem(&process_domains, &pid, &moved, BPF_ANY))
                lost_event();
            bpf_map_delete_elem(&process_domains, &former_tid);
            domain = bpf_map_lookup_elem(&process_domains, &pid);
        }
    }
    if (!domain)
        return 0;
    struct process_event *event = event_buffer();
    if (!event) {
        lost_event();
        return 0;
    }
    event->cookie = domain->cookie;
    event->birth_ns = domain->birth_ns;
    event->birth_pid = domain->birth_pid;
    event->kernel_time_ns = bpf_ktime_get_ns();
    event->type = EVENT_EXEC;
    event->pid = pid;
    event->former_tid = former_tid;
    bpf_get_current_comm(event->comm, sizeof(event->comm));
    __u32 offset = ctx->data_loc_filename & 0xffff;
    bpf_probe_read_str(event->filename, sizeof(event->filename), (void *)ctx + offset);
    submit(event);
    return 0;
}

SEC("tp/sched/sched_process_exit")
int handle_exit(struct sched_process_template_args *ctx)
{
    __u64 pid_tgid = bpf_get_current_pid_tgid();
    __u32 pid = (__u32)pid_tgid;
    struct process_domain *domain = domain_for_current(pid);
    if (!domain)
        return 0;
    struct process_event *event = event_buffer();
    if (event) {
        event->cookie = domain->cookie;
        event->birth_ns = domain->birth_ns;
        event->birth_pid = domain->birth_pid;
        event->kernel_time_ns = bpf_ktime_get_ns();
        event->type = EVENT_EXIT;
        event->pid = pid;
        __builtin_memcpy(event->comm, ctx->comm, TASK_COMM_LEN);
        submit(event);
    } else
        lost_event();
    bpf_map_delete_elem(&process_domains, &pid);
    return 0;
}

char LICENSE[] SEC("license") = "Dual BSD/GPL";
