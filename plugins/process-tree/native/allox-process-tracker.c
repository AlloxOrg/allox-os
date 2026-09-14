// SPDX-License-Identifier: Apache-2.0

#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <unistd.h>
#include <time.h>

#include <bpf/bpf.h>
#include <bpf/libbpf.h>
#include "allox_process.h"
#include "allox_process.skel.h"

static volatile sig_atomic_t exiting;
static int produced_fd;
struct counter {
    __u64 cookie;
    __u64 cgroup_id;
    __u64 seen;
    struct counter *next;
};
static struct counter *counters;

static struct bpf_link *attach_tracepoint(const struct bpf_program *program,
                                          const char *category, const char *name)
{
    struct bpf_link *link = bpf_program__attach_tracepoint(program, category, name);
    int error = libbpf_get_error(link);
    if (error) {
        fprintf(stderr, "attach tracepoint %s/%s: %s\n",
                category, name, strerror(-error));
        return NULL;
    }
    return link;
}

static struct counter *counter_for(__u64 cookie)
{
    for (struct counter *c = counters; c; c = c->next)
        if (c->cookie == cookie)
            return c;
    return NULL;
}

static void remove_counter(struct counter *counter)
{
    struct counter **cursor = &counters;
    while (*cursor && *cursor != counter)
        cursor = &(*cursor)->next;
    if (*cursor) {
        *cursor = counter->next;
        free(counter);
    }
}

static void on_signal(int signo)
{
    (void)signo;
    exiting = 1;
}

static void json_string(const char *value, size_t length)
{
    putchar('"');
    for (size_t i = 0; i < length && value[i]; ++i) {
        unsigned char ch = value[i];
        if (ch == '"' || ch == '\\') {
            putchar('\\');
            putchar(ch);
        } else if (ch >= 0x20 && ch < 0x7f) {
            putchar(ch);
        } else {
            printf("\\u%04x", ch);
        }
    }
    putchar('"');
}

static int handle_event(void *context, void *data, size_t length)
{
    (void)context;
    if (length < sizeof(struct process_event))
        return 0;
    const struct process_event *event = data;
    struct counter *counter = counter_for(event->cookie);
    if (counter)
        counter->seen++;
    const char *name = event->type == EVENT_FORK ? "fork" :
                       event->type == EVENT_EXEC ? "exec" :
                       event->type == EVENT_ROOT ? "root" : "exit";
    printf("{\"kind\":\"event\",\"event\":\"%s\",\"cookie\":%llu,"
           "\"kernel_time_ns\":%llu,\"birth_ns\":%llu,\"parent_birth_ns\":%llu,"
           "\"pid\":%u,\"ppid\":%u,\"former_tid\":%u,\"birth_pid\":%u,\"parent_birth_pid\":%u,"
           "\"comm\":", name, (unsigned long long)event->cookie,
           (unsigned long long)event->kernel_time_ns,
           (unsigned long long)event->birth_ns, (unsigned long long)event->parent_birth_ns,
           event->pid, event->ppid,
           event->former_tid, event->birth_pid, event->parent_birth_pid);
    json_string(event->comm, sizeof(event->comm));
    printf(",\"filename\":");
    json_string(event->filename, sizeof(event->filename));
    puts("}");
    return 0;
}

static int check_loss(int lost_fd)
{
    __u32 zero = 0;
    __u64 count = 0;
    if (bpf_map_lookup_elem(lost_fd, &zero, &count) || count) {
        puts("{\"kind\":\"error\",\"error\":\"BPF event or attribution loss\"}");
        return -EIO;
    }
    return 0;
}

static int cgroup_id_for_pid(unsigned int pid, __u64 *cgroup_id)
{
    char proc_path[64], line[4096], cgroup_path[4096];
    if (snprintf(proc_path, sizeof(proc_path), "/proc/%u/cgroup", pid) >=
        (int)sizeof(proc_path))
        return -ENAMETOOLONG;
    FILE *stream = fopen(proc_path, "r");
    if (!stream)
        return -errno;
    int rc = -ENOENT;
    while (fgets(line, sizeof(line), stream)) {
        char *relative = strstr(line, "0::");
        if (!relative)
            continue;
        relative += 3;
        relative[strcspn(relative, "\r\n")] = '\0';
        if (snprintf(cgroup_path, sizeof(cgroup_path), "/sys/fs/cgroup%s", relative) >=
            (int)sizeof(cgroup_path)) {
            rc = -ENAMETOOLONG;
            break;
        }
        struct stat info;
        if (stat(cgroup_path, &info)) {
            rc = -errno;
            break;
        }
        *cgroup_id = (__u64)info.st_ino;
        rc = 0;
        break;
    }
    fclose(stream);
    return rc;
}

static int control_line(int map_fd, int session_fd, int lost_fd,
                        struct ring_buffer *ring, const char *line)
{
    char operation[16] = {};
    unsigned int pid = 0;
    unsigned long long cookie = 0;
    if (sscanf(line, "%15s %u %llu", operation, &pid, &cookie) != 3 || !pid || !cookie) {
        puts("{\"kind\":\"error\",\"error\":\"invalid control message\"}");
        return -EINVAL;
    }
    int rc;
    if (!strcmp(operation, "seed")) {
        if (counter_for(cookie))
            return -EEXIST;
        struct counter *counter = calloc(1, sizeof(*counter));
        if (!counter)
            return -ENOMEM;
        counter->cookie = cookie;
        counter->next = counters;
        counters = counter;
        __u64 zero = 0, cookie_key = cookie;
        if (bpf_map_update_elem(produced_fd, &cookie_key, &zero, BPF_NOEXIST)) {
            remove_counter(counter);
            return -errno;
        }
        __u64 cgroup_id;
        rc = cgroup_id_for_pid(pid, &cgroup_id);
        if (rc) {
            bpf_map_delete_elem(produced_fd, &cookie_key);
            remove_counter(counter);
            return rc;
        }
        counter->cgroup_id = cgroup_id;
        struct timespec now;
        clock_gettime(CLOCK_MONOTONIC, &now);
        struct session_domain value = {
            .cookie = cookie, .birth_ns = (__u64)now.tv_sec * 1000000000 + now.tv_nsec,
        };
        rc = bpf_map_update_elem(session_fd, &cgroup_id, &value, BPF_ANY);
        if (rc) {
            int saved_errno = errno;
            bpf_map_delete_elem(produced_fd, &cookie_key);
            remove_counter(counter);
            errno = saved_errno;
        }
    } else if (!strcmp(operation, "finish")) {
        /* Called only once the owning cgroup is empty. Consume before ACK so
         * the adapter cannot complete a run ahead of its queued exit events. */
        struct counter *counter = counter_for(cookie);
        __u64 expected, cookie_key = cookie;
        if (!counter || bpf_map_lookup_elem(produced_fd, &cookie_key, &expected))
            return -EINVAL;
        /* A different CPU can temporarily block the head of a shared ring.
         * Use the kernel's per-run produced count, not a single empty poll. */
        for (int attempt = 0; counter->seen < expected && attempt < 200; ++attempt) {
            rc = ring_buffer__poll(ring, 20);
            if (rc < 0 && rc != -EINTR)
                return rc;
        }
        if (counter->seen != expected || check_loss(lost_fd))
            return -EIO;
        __u32 key, next;
        bool have_key = false;
        unsigned int visited = 0;
        while (visited++ < 131072 && !bpf_map_get_next_key(map_fd, have_key ? &key : NULL, &next)) {
            struct process_domain value;
            key = next;
            have_key = true;
            if (!bpf_map_lookup_elem(map_fd, &key, &value) && value.cookie == cookie) {
                bpf_map_delete_elem(map_fd, &key);
                have_key = false;
            }
        }
        if (visited >= 131072)
            return -EAGAIN;
        bpf_map_delete_elem(produced_fd, &cookie_key);
        bpf_map_delete_elem(session_fd, &counter->cgroup_id);
        remove_counter(counter);
        rc = 0;
    } else {
        puts("{\"kind\":\"error\",\"error\":\"unknown control operation\"}");
        return -EINVAL;
    }
    if (rc) {
        puts("{\"kind\":\"error\",\"error\":\"BPF map update failed\"}");
        return -errno;
    }
    printf("{\"kind\":\"ack\",\"operation\":\"%s\",\"cookie\":%llu}\n",
           operation, cookie);
    return 0;
}

static int drain_control(int map_fd, int session_fd, int lost_fd, struct ring_buffer *ring,
                         char *buffer, size_t *used)
{
    ssize_t count = read(STDIN_FILENO, buffer + *used, 4095 - *used);
    if (count == 0)
        return 1;
    if (count < 0) {
        if (errno == EAGAIN || errno == EINTR)
            return 0;
        return -errno;
    }
    *used += (size_t)count;
    buffer[*used] = '\0';
    char *start = buffer;
    for (;;) {
        char *newline = strchr(start, '\n');
        if (!newline)
            break;
        *newline = '\0';
        if (control_line(map_fd, session_fd, lost_fd, ring, start))
            return -EINVAL;
        start = newline + 1;
    }
    size_t remaining = *used - (size_t)(start - buffer);
    memmove(buffer, start, remaining);
    *used = remaining;
    if (*used == 4095)
        return -E2BIG;
    return 0;
}

int main(void)
{
    struct rlimit limit = {RLIM_INFINITY, RLIM_INFINITY};
    setrlimit(RLIMIT_MEMLOCK, &limit);
    signal(SIGINT, on_signal);
    signal(SIGTERM, on_signal);
    setvbuf(stdout, NULL, _IOLBF, 0);
    int flags = fcntl(STDIN_FILENO, F_GETFL, 0);
    if (flags < 0 || fcntl(STDIN_FILENO, F_SETFL, flags | O_NONBLOCK) < 0)
        return 1;

    struct allox_process_bpf *skeleton = allox_process_bpf__open_and_load();
    if (!skeleton) {
        fputs("failed to load Allox BPF object\n", stderr);
        return 1;
    }
    skeleton->links.handle_fork = attach_tracepoint(
        skeleton->progs.handle_fork, "sched", "sched_process_fork");
    skeleton->links.handle_exec = attach_tracepoint(
        skeleton->progs.handle_exec, "sched", "sched_process_exec");
    skeleton->links.handle_exit = attach_tracepoint(
        skeleton->progs.handle_exit, "sched", "sched_process_exit");
    if (!skeleton->links.handle_fork || !skeleton->links.handle_exec ||
        !skeleton->links.handle_exit) {
        fputs("failed to attach Allox sched tracepoints\n", stderr);
        allox_process_bpf__destroy(skeleton);
        return 1;
    }
    int rc;
    struct ring_buffer *ring = ring_buffer__new(
        bpf_map__fd(skeleton->maps.events), handle_event, NULL, NULL);
    if (!ring) {
        allox_process_bpf__destroy(skeleton);
        return 1;
    }
    puts("{\"kind\":\"ready\",\"abi\":1}");
    char input[4096] = {};
    size_t used = 0;
    int map_fd = bpf_map__fd(skeleton->maps.process_domains);
    int session_fd = bpf_map__fd(skeleton->maps.session_domains);
    int lost_fd = bpf_map__fd(skeleton->maps.lost);
    produced_fd = bpf_map__fd(skeleton->maps.produced);
    while (!exiting) {
        rc = ring_buffer__poll(ring, 20);
        if (rc < 0 && rc != -EINTR)
            break;
        if (check_loss(lost_fd)) {
            rc = -EIO;
            break;
        }
        rc = drain_control(map_fd, session_fd, lost_fd, ring, input, &used);
        if (rc != 0)
            break;
    }
    ring_buffer__free(ring);
    allox_process_bpf__destroy(skeleton);
    return rc < 0 ? 1 : 0;
}
