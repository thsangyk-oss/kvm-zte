// SPDX-License-Identifier: GPL-2.0
/*
 * xd-venc (W105D): MJPEG (card capture MS2109) -> H.264 bang HCodec cua S905X2,
 * qua /dev/amvenc_avc (module amvenc_avc tu meson-sm1-codecs + ban va G12A, xem README.md).
 * Phan giao tiep voi encoder lay tu venc.c cua meson-sm1-codecs (GPL-2.0).
 *
 * Giao thuc pipe giong xd-venc cua W152D (kvmweb dung chung):
 *   stdin  (kvmweb -> xd-venc), moi ban ghi bat dau bang u32 LE "hdr":
 *     hdr & 0xC0000000 == 0          : hdr & 0x3FFFFFFF byte JPEG theo sau
 *     hdr & 0x80000000               : nhu tren + ep khung IDR (nguoi xem moi / mat goi)
 *     hdr & 0xC0000000 == 0x40000000 : doi bitrate = (hdr & 0x3FFFFFFF) kbit/s (khong co payload)
 *   stdout (xd-venc -> kvmweb): u32 LE len, u8 flags (bit0 = IDR), u16 LE w, u16 LE h, roi len byte Annex-B
 *     (moi IDR kem SPS/PPS).
 * Luong: doc + giai ma JPEG (libjpeg-turbo -> NV12) o luong chinh, ma hoa o luong rieng;
 * encoder ban thi chi giu khung moi nhat (khong xep hang -> do tre thap).
 *
 * Build: gcc -O2 -Wall -o xd-venc xd-venc.c -lturbojpeg -lpthread
 */
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <unistd.h>
#include <turbojpeg.h>

#define IOC(n)			_IOW('E', n, uint32_t)
#define IOC_NEW_CMD		IOC(0x02)
#define IOC_GET_STAGE		IOC(0x03)
#define IOC_GET_OUTPUT_SIZE	IOC(0x04)
#define IOC_CONFIG_INIT		IOC(0x05)
#define IOC_GET_BUFFINFO	IOC(0x08)
#define IOC_SUBMIT		IOC(0x09)

enum { SEQUENCE = 1, IDR = 3, NON_IDR = 4, PICTURE_DONE = 8, IDR_DONE = 9, NON_IDR_DONE = 10 };
#define FMT_NV12	3
#define LOCAL_BUFF	0
#define FLUSH_INPUT	0x1
#define FLUSH_OUTPUT	0x2
#define FLUSH_CBR	0x80
#define CBR_TABLE_SIZE	0x800
#define ALIGN(x, a)	(((x) + (a) - 1) / (a) * (a))
#define MAX_W		1920
#define MAX_H		1088
#define GOP		(fps * 4)          /* IDR dinh ky moi ~4 s (nguoi xem moi / mat goi thi ep IDR ngay) */
#define NV_MAX		(MAX_W * MAX_H * 3 / 2)

static pthread_mutex_t mu = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t cv = PTHREAD_COND_INITIALIZER;
static uint8_t *pend, *work;            /* NV12 khung cho / dang ma hoa (chat, pitch = w) */
static int pw, ph, have, want_idr, quit, kbps = 6000, fps = 30;

static int rd(void *b, size_t n)
{
	uint8_t *p = b;
	while (n) {
		ssize_t r = read(0, p, n);
		if (r <= 0) return -1;
		p += r; n -= r;
	}
	return 0;
}

static void wr(const void *b, size_t n)
{
	const uint8_t *p = b;
	while (n) {
		ssize_t r = write(1, p, n);
		if (r <= 0) { quit = 1; return; }
		p += r; n -= r;
	}
}

/* YUV planar (tjDecompressToYUV2, pad 1) -> NV12; 4:2:2 bo 1 hang chroma moi 2 hang */
static void to_nv12(const uint8_t *yuv, int w, int h, int ss, uint8_t *nv)
{
	int cw = tjPlaneWidth(1, w, ss), ch = tjPlaneHeight(1, h, ss);
	const uint8_t *Y = yuv, *U = Y + (size_t)w * h, *V = U + (size_t)cw * ch;
	memcpy(nv, Y, (size_t)w * h);
	uint8_t *uv = nv + (size_t)w * h;
	int step = (ss == TJSAMP_422) ? 2 : 1;
	for (int r = 0; r < h / 2; r++) {
		const uint8_t *u = U + (size_t)r * step * cw, *v = V + (size_t)r * step * cw;
		uint8_t *o = uv + (size_t)r * w;
		for (int x = 0; x < w / 2; x++) { o[2 * x] = u[x]; o[2 * x + 1] = v[x]; }
	}
}

/* ---------------- encoder (/dev/amvenc_avc) ---------------- */
struct enc {
	int fd, w, h, n;
	uint32_t pitch, vh, dct_off, bs_off, bs_size, cbr_off, bufsize, hdr_len;
	uint8_t *mem, hdr[256];
	double qf;
};

static uint32_t run(int fd, uint32_t *a)
{
	struct pollfd p = { .fd = fd, .events = POLLIN };
	uint32_t stage = 0;
	if (ioctl(fd, IOC_NEW_CMD, a)) return 0;
	if (poll(&p, 1, 3000) <= 0) return 0;
	ioctl(fd, IOC_GET_STAGE, &stage);
	return stage;
}

static uint32_t out_size(int fd)
{
	uint32_t o[4] = { 0 };
	ioctl(fd, IOC_GET_OUTPUT_SIZE, o);
	return o[0];
}

static void enc_close(struct enc *e)
{
	if (e->mem && e->mem != MAP_FAILED) munmap(e->mem, e->bufsize);
	if (e->fd >= 0) close(e->fd);
	memset(e, 0, sizeof(*e)); e->fd = -1;
}

static int enc_open(struct enc *e, int w, int h)
{
	uint32_t a[56] = { 0 };
	enc_close(e);
	e->w = w; e->h = h; e->pitch = ALIGN(w, 32); e->vh = ALIGN(h, 16); e->qf = 28;
	e->fd = open("/dev/amvenc_avc", O_RDWR);
	if (e->fd < 0) { fprintf(stderr, "xd-venc: loi mo /dev/amvenc_avc: %s\n", strerror(errno)); return -1; }
	a[0] = 0; a[1] = e->vh / 16; a[2] = w; a[3] = h;           /* UCODE_MODE_FULL, 1 slice */
	if (ioctl(e->fd, IOC_CONFIG_INIT, a)) { fprintf(stderr, "xd-venc: loi CONFIG_INIT %dx%d\n", w, h); goto bad; }
	e->dct_off = a[1]; e->bs_off = a[3]; e->bs_size = a[4]; e->cbr_off = a[9];
	ioctl(e->fd, IOC_GET_BUFFINFO, &e->bufsize);
	e->mem = mmap(NULL, e->bufsize, PROT_READ | PROT_WRITE, MAP_SHARED, e->fd, 0);
	if (e->mem == MAP_FAILED) { fprintf(stderr, "xd-venc: loi mmap\n"); goto bad; }
	memset(a, 0, sizeof(a));
	a[0] = SEQUENCE; a[2] = 28; a[3] = FLUSH_OUTPUT;           /* SPS + PPS, lap lai truoc moi IDR */
	if (run(e->fd, a) != PICTURE_DONE) { fprintf(stderr, "xd-venc: loi tao SPS/PPS\n"); goto bad; }
	uint32_t n = out_size(e->fd);
	e->hdr_len = (n >> 16) + (n & 0xffff);
	if (e->hdr_len > sizeof(e->hdr)) goto bad;
	memcpy(e->hdr, e->mem + e->bs_off, e->hdr_len);
	fprintf(stderr, "xd-venc: encoder %dx%d %d kbit/s\n", w, h, kbps);
	return 0;
bad:
	enc_close(e);
	return -1;
}

/* ma hoa 1 khung NV12 chat (pitch = w) -> ghi ra stdout; tra -1 neu encoder loi */
static int enc_frame(struct enc *e, const uint8_t *nv, int idr, int k)
{
	uint8_t *y = e->mem + e->dct_off, *c = y + e->pitch * e->vh;
	uint32_t a[56] = { 0 }, cmd = idr ? IDR : NON_IDR, stage, sz, fq;
	const uint8_t *uv = nv + (size_t)e->w * e->h;
	for (uint32_t r = 0; r < e->vh; r++)
		memcpy(y + r * e->pitch, nv + (size_t)(r < (uint32_t)e->h ? r : e->h - 1) * e->w, e->w);
	for (uint32_t r = 0; r < e->vh / 2; r++)
		memcpy(c + r * e->pitch, uv + (size_t)(r < (uint32_t)e->h / 2 ? r : e->h / 2 - 1) * e->w, e->w);
	fq = (uint32_t)(e->qf + 0.5);
	memset(e->mem + e->cbr_off, fq, CBR_TABLE_SIZE);          /* ucode lay QP tu bang CBR */
	a[0] = cmd; a[2] = LOCAL_BUFF; a[3] = FMT_NV12; a[5] = e->pitch * e->vh * 3 / 2; a[6] = fq;
	a[7] = FLUSH_INPUT | FLUSH_OUTPUT | FLUSH_CBR; a[8] = 2000; a[13] = e->w; a[14] = e->h;
	stage = run(e->fd, a);
	sz = out_size(e->fd);
	if ((stage != IDR_DONE && stage != NON_IDR_DONE) || !sz || sz > e->bs_size) {
		fprintf(stderr, "xd-venc: loi ma hoa khung (stage %u, %u byte)\n", stage, sz);
		return -1;
	}
	uint32_t len = sz + (idr ? e->hdr_len : 0);
	uint8_t hd[9] = { len, len >> 8, len >> 16, len >> 24, (uint8_t)idr, e->w, e->w >> 8, e->h, e->h >> 8 };
	wr(hd, 9);
	if (idr) wr(e->hdr, e->hdr_len);
	wr(e->mem + e->bs_off, sz);
	ioctl(e->fd, IOC_SUBMIT, &cmd);
	/* RC theo khung (venc.c): thang log, 6 QP = 2x bit; IDR duoc 4x ngan sach */
	double target = (double)k * 1000 / 8 / fps;
	double ratio = sz / (idr ? 4.0 * target : target);
	if (ratio > 1.15 || ratio < 0.87)
		e->qf += (ratio > 1.0 ? 1.0 : -1.0) * (ratio > 2.0 || ratio < 0.5 ? 2.0 : 0.5);
	e->qf = e->qf < 12 ? 12 : e->qf > 51 ? 51 : e->qf;
	return 0;
}

static void *enc_thread(void *arg)
{
	(void)arg;
	struct enc e = { .fd = -1 };
	int fails = 0;
	for (;;) {
		pthread_mutex_lock(&mu);
		while (!have && !quit) pthread_cond_wait(&cv, &mu);
		if (quit) { pthread_mutex_unlock(&mu); break; }
		uint8_t *t = work; work = pend; pend = t;
		int w = pw, h = ph, idr = want_idr, k = kbps;
		have = 0; want_idr = 0;
		pthread_mutex_unlock(&mu);

		if (e.fd < 0 || w != e.w || h != e.h) {
			if (enc_open(&e, w, h)) {
				if (++fails > 5) { fprintf(stderr, "xd-venc: loi encoder lien tuc, thoat\n"); quit = 1; break; }
				sleep(1); continue;
			}
			idr = 1;
		}
		if (e.n % GOP == 0) idr = 1;
		if (enc_frame(&e, work, idr, k)) {
			enc_close(&e);
			if (++fails > 5) { quit = 1; break; }
			continue;
		}
		fails = 0; e.n = idr ? 1 : e.n + 1;
		if (quit) break;
	}
	enc_close(&e);
	close(1);
	return NULL;
}

int main(int argc, char **argv)
{
	if (argc > 1) kbps = atoi(argv[1]);
	if (argc > 2) fps = atoi(argv[2]);
	if (kbps < 200) kbps = 200;
	if (fps < 1 || fps > 60) fps = 30;
	signal(SIGPIPE, SIG_IGN);
	tjhandle tj = tjInitDecompress();
	size_t cap = 0, ycap = 0;
	uint8_t *jpg = NULL, *yuv = NULL;
	int warned = 0;
	pend = malloc(NV_MAX); work = malloc(NV_MAX);
	pthread_t th;
	pthread_create(&th, NULL, enc_thread, NULL);
	while (!quit) {
		uint32_t hdr;
		if (rd(&hdr, 4)) break;
		if ((hdr & 0xC0000000u) == 0x40000000u) {
			pthread_mutex_lock(&mu); kbps = hdr & 0x3FFFFFFF; if (kbps < 200) kbps = 200; pthread_mutex_unlock(&mu);
			continue;
		}
		int idr = !!(hdr & 0x80000000u);
		size_t n = hdr & 0x3FFFFFFF;
		if (n > (16u << 20)) break;
		if (n > cap) { cap = n; jpg = realloc(jpg, cap); }
		if (rd(jpg, n)) break;
		int w, h, ss, cs;
		if (tjDecompressHeader3(tj, jpg, n, &w, &h, &ss, &cs) || (ss != TJSAMP_422 && ss != TJSAMP_420)) continue;
		w &= ~1; h &= ~1;
		if (w > MAX_W || h > MAX_H || w < 64 || h < 64) {
			if (!warned++) fprintf(stderr, "xd-venc: loi do phan giai %dx%d (H.264 phan cung toi da %dx%d)\n", w, h, MAX_W, MAX_H);
			continue;
		}
		warned = 0;
		size_t ys = tjBufSizeYUV2(w, 1, h, ss);
		if (ys > ycap) { ycap = ys; yuv = realloc(yuv, ycap); }
		if (tjDecompressToYUV2(tj, jpg, n, yuv, w, 1, h, TJFLAG_FASTDCT)) continue;
		pthread_mutex_lock(&mu);
		to_nv12(yuv, w, h, ss, pend);
		pw = w; ph = h; have = 1; want_idr |= idr;
		pthread_cond_signal(&cv);
		pthread_mutex_unlock(&mu);
	}
	pthread_mutex_lock(&mu); quit = 1; pthread_cond_signal(&cv); pthread_mutex_unlock(&mu);
	pthread_join(th, NULL);
	return 0;
}
