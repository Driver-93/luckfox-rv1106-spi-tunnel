/* usbat3 - AT only on iface 3/4, multi-URB read until OK/ERROR */
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

static int find_dev(const char *vidpid, char *out, size_t n, char *cfg, size_t cn) {
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
            unsigned char dev[18];
            ct.data = dev; ct.wValue = 0x0100; ct.wLength = 18;
            if (ioctl(fd, USBDEVFS_CONTROL, &ct) < 18) { close(fd); continue; }
            unsigned short v = dev[8] | (dev[9] << 8);
            unsigned short p = dev[10] | (dev[11] << 8);
            char vp[16];
            snprintf(vp, sizeof(vp), "%04x:%04x", v, p);
            if (strcasecmp(vp, vidpid) == 0) {
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

int main(int argc, char **argv) {
    const char *vidpid = argc > 1 ? argv[1] : "2c7c:0903";
    const char *cmd = argc > 2 ? argv[2] : "AT";
    char path[512];
    if (find_dev(vidpid, path, sizeof(path)) < 0) {
        fprintf(stderr, "device not found\n");
        return 1;
    }
    int fd = open(path, O_RDWR);
    if (fd < 0) { perror("open"); return 1; }
    FILE *f = fopen("/tmp/usbconf.bin", "rb");
    unsigned char d[4096];
    size_t len = f ? fread(d, 1, sizeof(d), f) : 0;
    if (f) fclose(f);

    size_t i = 0;
    int cur = -1;
    while (i + 2 <= len) {
        int blen = d[i], btype = d[i + 1];
        if (blen < 2 || i + blen > len) break;
        if (btype == 4) {
            cur = d[i + 2];
            int neps = d[i + 4];
            int eps[16], ne = 0, cnt = 0;
            size_t j = i + blen;
            while (cnt < neps && j + 2 <= len) {
                int l2 = d[j], t2 = d[j + 1];
                if (t2 == 5) {
                    int ep = d[j + 2], attr = d[j + 3] & 3;
                    if (attr == 2 && ne < 16) eps[ne++] = ep;
                    cnt++;
                } else if (t2 == 4) break;
                j += l2;
                if (l2 < 2) break;
            }
            /* only class-255 vendor interfaces with exactly 2 bulk eps = AT/modem ports */
            if (ne == 2 && d[i + 5] == 255) {
                int ep_in = 0, ep_out = 0, k;
                for (k = 0; k < 2; k++) {
                    if (eps[k] & 0x80) ep_in = eps[k]; else ep_out = eps[k];
                }
                if (ioctl(fd, USBDEVFS_CLAIMINTERFACE, &cur) < 0) { i += blen; continue; }
                char txbuf[128];
                int txlen = snprintf(txbuf, sizeof(txbuf), "%s\r", cmd);
                struct usbdevfs_urb wo = {0};
                wo.type = USBDEVFS_URB_TYPE_BULK; wo.endpoint = ep_out;
                wo.buffer = txbuf; wo.buffer_length = txlen;
                if (ioctl(fd, USBDEVFS_SUBMITURB, &wo) == 0 &&
                    ioctl(fd, USBDEVFS_REAPURB, &wo) == 0) {
                    /* read until OK/ERROR/CME, max ~6s */
                    char acc[4096] = {0};
                    size_t accn = 0;
                    for (int rd = 0; rd < 60; rd++) {
                        struct usbdevfs_urb ro = {0};
                        unsigned char rxbuf[1024];
                        ro.type = USBDEVFS_URB_TYPE_BULK; ro.endpoint = ep_in;
                        ro.buffer = rxbuf; ro.buffer_length = sizeof(rxbuf);
                        if (ioctl(fd, USBDEVFS_SUBMITURB, &ro) < 0) break;
                        int got = 0;
                        for (int t = 0; t < 20; t++) {
                            if (ioctl(fd, USBDEVFS_REAPURBNDELAY, &ro) >= 0) { got = 1; break; }
                            if (errno != EAGAIN) break;
                            usleep(50000);
                        }
                        if (!got) { ioctl(fd, USBDEVFS_DISCARDURB, &ro); break; }
                        if (accn + ro.actual_length < sizeof(acc)) {
                            memcpy(acc + accn, rxbuf, ro.actual_length);
                            accn += ro.actual_length;
                            acc[accn] = 0;
                        }
                        if (strstr(acc, "\r\nOK") || strstr(acc, "\r\nERROR") ||
                            strstr(acc, "CME ERROR") || strstr(acc, "CMS ERROR")) break;
                    }
                    printf("== iface %d (ep %02x/%02x):\n%.1000s\n", cur, ep_out, ep_in, acc);
                    ioctl(fd, USBDEVFS_RELEASEINTERFACE, &cur);
                    if (accn > 2) { close(fd); return 0; }
                } else {
                    ioctl(fd, USBDEVFS_RELEASEINTERFACE, &cur);
                }
            }
        }
        i += blen;
    }
    close(fd);
    printf("(no AT response on any port)\n");
    return 2;
}
