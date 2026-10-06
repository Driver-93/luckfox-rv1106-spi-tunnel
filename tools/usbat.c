/* usbat2 - parse config descriptor properly, probe interfaces 0..4 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>
#include <dirent.h>
#include <fcntl.h>
#include <unistd.h>
#include <errno.h>
#include <sys/ioctl.h>
#include <linux/usbdevice_fs.h>

static int find_dev(const char *vidpid, char *out, size_t n) {
    DIR *b = opendir("/dev/bus/usb");
    if (!b) return -1;
    struct dirent *be, *de;
    while ((be = readdir(b))) {
        char busdir[256];
        snprintf(busdir, sizeof(busdir), "/dev/bus/usb/%s", be->d_name);
        DIR *d = opendir(busdir);
        if (!d) continue;
        while ((de = readdir(d))) {
            char path[512];
            snprintf(path, sizeof(path), "%s/%s", busdir, de->d_name);
            int fd = open(path, O_RDWR);
            if (fd < 0) continue;
            unsigned char desc[4096];
            struct usbdevfs_ctrltransfer ct;
            memset(&ct, 0, sizeof(ct));
            ct.bRequestType = 0x80; ct.bRequest = 0x06;
            ct.wValue = 0x0200; ct.wIndex = 0; ct.wLength = sizeof(desc);
            ct.timeout = 1000; ct.data = desc;
            int len = ioctl(fd, USBDEVFS_CONTROL, &ct);
            if (len < 4) { close(fd); continue; }
            /* fetch DEVICE descriptor for VID/PID */
            unsigned char dev[18];
            ct.data = dev; ct.wValue = 0x0100; ct.wLength = 18;
            if (ioctl(fd, USBDEVFS_CONTROL, &ct) < 18) { close(fd); continue; }
            unsigned short v = dev[8] | (dev[9] << 8);
            unsigned short p = dev[10] | (dev[11] << 8);
            char vp[16];
            snprintf(vp, sizeof(vp), "%04x:%04x", v, p);
            if (strcasecmp(vp, vidpid) == 0) {
                /* save config descriptor for later */
                FILE *f = fopen("/tmp/usbconf.bin", "wb");
                if (f) { fwrite(desc, 1, len, f); fclose(f); }
                close(fd);
                snprintf(out, n, "%s", path);
                closedir(d); closedir(b);
                return 0;
            }
            close(fd);
        }
        closedir(d);
    }
    closedir(b);
    return -1;
}

/* parse config descriptor: dump all interfaces + endpoints */
static void dump_desc(void) {
    FILE *f = fopen("/tmp/usbconf.bin", "rb");
    if (!f) return;
    unsigned char d[4096];
    size_t len = fread(d, 1, sizeof(d), f);
    fclose(f);
    size_t i = 0;
    int cur_iface = -1;
    printf("--- config descriptor ---\n");
    while (i + 2 <= len) {
        int blen = d[i], btype = d[i + 1];
        if (blen < 2 || i + blen > len) break;
        if (btype == 4) { /* interface */
            cur_iface = d[i + 2];
            printf("iface %d alt %d class %d neps %d\n", cur_iface, d[i+3], d[i+5], d[i+4]);
        } else if (btype == 5) { /* endpoint */
            int ep = d[i + 2], attr = d[i + 3] & 3, mps = d[i+4] | (d[i+5]<<8);
            printf("  iface %d ep 0x%02x attr %d mps %d\n", cur_iface, ep, attr, mps);
        }
        i += blen;
    }
}

int main(int argc, char **argv) {
    const char *vidpid = argc > 1 ? argv[1] : "2c7c:0903";
    const char *cmd = argc > 2 ? argv[2] : "AT";

    char path[512];
    if (find_dev(vidpid, path, sizeof(path)) < 0) {
        fprintf(stderr, "device %s not found\n", vidpid);
        return 1;
    }
    printf("device: %s\n", path);
    dump_desc();

    FILE *f = fopen("/tmp/usbconf.bin", "rb");
    unsigned char d[4096];
    size_t len = f ? fread(d, 1, sizeof(d), f) : 0;
    if (f) fclose(f);

    int fd = open(path, O_RDWR);
    if (fd < 0) { perror("open"); return 1; }

    /* try every interface that has 2 bulk endpoints */
    size_t i = 0;
    int cur_iface = -1;
    int tried = 0;
    while (i + 2 <= len) {
        int blen = d[i], btype = d[i + 1];
        if (blen < 2 || i + blen > len) break;
        if (btype == 4) {
            cur_iface = d[i + 2];
            int neps = d[i + 4];
            /* collect this interface's endpoints */
            int eps[16][2]; int ne = 0; /* [addr, is_bulk_in] */
            size_t j = i + blen;
            int cnt = 0;
            while (cnt < neps && j + 2 <= len) {
                int l2 = d[j], t2 = d[j + 1];
                if (t2 == 5) {
                    int ep = d[j + 2], attr = d[j + 3] & 3;
                    if (attr == 2 && ne < 16) {
                        eps[ne][0] = ep;
                        eps[ne][1] = (ep & 0x80) ? 1 : 0;
                        ne++;
                    }
                    cnt++;
                } else if (t2 == 4) break;
                j += l2;
                if (l2 < 2) break;
            }
            if (ne == 2) {
                int ep_in = 0, ep_out = 0, k;
                for (k = 0; k < 2; k++) {
                    if (eps[k][1]) ep_in = eps[k][0]; else ep_out = eps[k][0];
                }
                printf("try iface %d ep_out=0x%02x ep_in=0x%02x ... ", cur_iface, ep_out, ep_in);
                fflush(stdout);
                if (ioctl(fd, USBDEVFS_CLAIMINTERFACE, &cur_iface) < 0) {
                    printf("claim failed: %s\n", strerror(errno));
                    i += blen; continue;
                }
                char txbuf[64];
                int txlen = snprintf(txbuf, sizeof(txbuf), "%s\r", cmd);
                unsigned char rxbuf[512];
                struct usbdevfs_urb wo = {0}, ro = {0};
                wo.type = USBDEVFS_URB_TYPE_BULK; wo.endpoint = ep_out;
                wo.buffer = txbuf; wo.buffer_length = txlen;
                if (ioctl(fd, USBDEVFS_SUBMITURB, &wo) < 0) {
                    printf("submit out failed: %s\n", strerror(errno));
                    ioctl(fd, USBDEVFS_RELEASEINTERFACE, &cur_iface);
                    i += blen; continue;
                }
                if (ioctl(fd, USBDEVFS_REAPURB, &wo) < 0) {
                    printf("reap out failed\n");
                    ioctl(fd, USBDEVFS_DISCARDURB, &wo);
                    ioctl(fd, USBDEVFS_RELEASEINTERFACE, &cur_iface);
                    i += blen; continue;
                }
                ro.type = USBDEVFS_URB_TYPE_BULK; ro.endpoint = ep_in;
                ro.buffer = rxbuf; ro.buffer_length = sizeof(rxbuf);
                if (ioctl(fd, USBDEVFS_SUBMITURB, &ro) < 0) {
                    printf("submit in failed: %s\n", strerror(errno));
                    ioctl(fd, USBDEVFS_RELEASEINTERFACE, &cur_iface);
                    i += blen; continue;
                }
                int got = 0;
                for (int t = 0; t < 40; t++) {
                    if (ioctl(fd, USBDEVFS_REAPURBNDELAY, &ro) >= 0) { got = 1; break; }
                    if (errno != EAGAIN) break;
                    usleep(50000);
                }
                if (got) {
                    int is_at = 0;
                    for (int h = 0; h + 1 < ro.actual_length; h++) {
                        if ((rxbuf[h] == 'O' && rxbuf[h+1] == 'K') ||
                            (rxbuf[h] == 'A' && rxbuf[h+1] == 'T')) is_at = 1;
                    }
                    printf("\n*** iface %d RESPONSE (%d bytes) %s\n",
                           cur_iface, ro.actual_length, is_at ? "*** AT-OK ***" : "(binary)");
                    int pr = ro.actual_length < 64 ? ro.actual_length : 64;
                    for (int h = 0; h < pr; h++) {
                        unsigned char c = rxbuf[h];
                        printf("%02x(%c) ", c, isprint(c) ? c : '.');
                    }
                    printf("\n");
                    ioctl(fd, USBDEVFS_RELEASEINTERFACE, &cur_iface);
                    if (is_at) { close(fd); return 0; }
                    tried++;
                }
                printf("no response\n");
                ioctl(fd, USBDEVFS_DISCARDURB, &ro);
                ioctl(fd, USBDEVFS_RELEASEINTERFACE, &cur_iface);
                tried++;
            }
        }
        i += blen;
    }
    if (!tried) printf("no dual-bulk interfaces found\n");
    close(fd);
    return 2;
}
