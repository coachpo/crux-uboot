// SPDX-License-Identifier: GPL-2.0+
/*
 * Crux PE install handoff (crux-install/1)
 *
 * Runs in the physical Recovery container's U-Boot after "reboot recovery".
 * The ROM requests an install through /crux_ota/request on pemetadata. The
 * installer and this command keep their progress in a CRC-protected state
 * block in the unused tail of pesystem and stage the FITs next to it; this
 * command lands a staged FIT into its cache window, reads it back and checks
 * that every other cache range is unchanged. It also passes the ABL device
 * identity through to the kernel instead of the identity captured in a FIT.
 * The layout and failure stops are described in the Crux workspace document
 * docs/pe13-install-protocol-2026-11-07.md.
 */

#include <blk.h>
#include <command.h>
#include <env.h>
#include <fs.h>
#include <malloc.h>
#include <mapmem.h>
#include <memalign.h>
#include <part.h>
#include <vsprintf.h>
#include <asm/unaligned.h>
#include <dm/uclass-id.h>
#include <linux/ctype.h>
#include <linux/libfdt.h>
#include <linux/string.h>
#include <u-boot/crc.h>

#define CRUX_BLKSZ		4096
#define CRUX_PESYSTEM_LBA	0x29e4000UL
#define CRUX_PESYSTEM_BLKS	0x100000UL
#define CRUX_STATE_LBA		(CRUX_PESYSTEM_LBA + 0xf0000UL)
#define CRUX_CACHE_LBA		0x30000UL
#define CRUX_CACHE_BLKS		0x18000UL
#define CRUX_TWRP_INDEX		2
#define CRUX_TWRP_CRC		0xeffcee45U
#define CRUX_STAGE_ADDR		0xc0000000UL
#define CRUX_VERIFY_ADDR	0xd0000000UL
#define CRUX_CHUNK_BLKS		0x1000UL
#define CRUX_MAX_TRIES		2
#define CRUX_VAL_MAX		48
#define CRUX_REQUEST		"/crux_ota/request"

struct crux_window {
	const char *name;
	ulong stage_lba;
	ulong lba;
	ulong blks;
};

static const struct crux_window crux_windows[] = {
	{ "recovery", CRUX_PESYSTEM_LBA + 0xf8000UL, 0x38000UL, 0x3b00UL },
	{ "rom", CRUX_PESYSTEM_LBA + 0xf1000UL, 0x3c000UL, 0x3100UL },
};

/* Cache ranges outside both PE windows; each landing leaves them unchanged. */
static const struct {
	ulong lba;
	ulong blks;
} crux_neighbors[] = {
	{ 0x30000, 0x28bd },	/* MIUI */
	{ 0x328bd, 0x1743 },
	{ 0x34000, 0x2924 },	/* retained TWRP, CRUX_TWRP_CRC */
	{ 0x36924, 0x16dc },
	{ 0x3bb00, 0x500 },
	{ 0x3f100, 0x8f00 },
};

/* Device and board identity taken from the ABL command line, never a FIT. */
static const char *const crux_identity[] = {
	"androidboot.serialno", "androidboot.cpuid", "androidboot.oled_wp",
	"androidboot.hwversion", "androidboot.hwlevel", "androidboot.hwc",
	"androidboot.ufsid", "androidboot.dp", "androidboot.baseband",
	"msm_drm.dsi_display0",
};

enum { S_VER, S_TXN, S_STAGE, S_TRIES, S_ROM_CRC, S_REC, S_REC_CRC, S_MSG, S_COUNT };
static const char *const crux_state_keys[S_COUNT] = {
	"crux_s_ver", "crux_s_txn", "crux_s_stage", "crux_s_tries",
	"crux_s_rom_crc", "crux_s_rec", "crux_s_rec_crc", "crux_s_msg",
};

enum { R_VER, R_TXN, R_ACTION, R_COUNT };
static const char *const crux_request_keys[R_COUNT] = {
	"crux_r_ver", "crux_r_txn", "crux_r_action",
};

static bool crux_value_ok(const char *v, size_t len)
{
	size_t i;

	if (len >= CRUX_VAL_MAX)
		return false;
	for (i = 0; i < len; i++)
		if (!isalnum(v[i]) && v[i] != '.' && v[i] != '_' && v[i] != '-')
			return false;
	return true;
}

/* Parse a 4096-byte "env -c" blob, keeping only the listed variables. */
static int crux_parse(const u8 *blob, const char *const *keys, int n,
		      char vals[][CRUX_VAL_MAX])
{
	const char *p = (const char *)blob + 4;
	const char *end = (const char *)blob + CRUX_BLKSZ;
	int i;

	for (i = 0; i < n; i++)
		vals[i][0] = '\0';
	if (crc32(0, blob + 4, CRUX_BLKSZ - 4) != get_unaligned_le32(blob))
		return -EBADMSG;
	while (p < end && *p) {
		size_t len = strnlen(p, end - p);
		const char *eq = memchr(p, '=', len);

		if (p + len == end || !eq)
			return -EBADMSG;
		for (i = 0; i < n; i++) {
			size_t klen = strlen(keys[i]);
			const char *v = eq + 1;
			size_t vlen = p + len - v;

			if (eq - p != klen || strncmp(p, keys[i], klen))
				continue;
			if (!crux_value_ok(v, vlen))
				return -EINVAL;
			memcpy(vals[i], v, vlen);
			vals[i][vlen] = '\0';
		}
		p += len + 1;
	}
	return 0;
}

static void crux_build(u8 *blob, const char *const *keys, int n,
		       char vals[][CRUX_VAL_MAX])
{
	char *p = (char *)blob + 4;
	int i;

	memset(blob, 0, CRUX_BLKSZ);
	for (i = 0; i < n; i++)
		if (vals[i][0])
			p += sprintf(p, "%s=%s", keys[i], vals[i]) + 1;
	put_unaligned_le32(crc32(0, blob + 4, CRUX_BLKSZ - 4), blob);
}

static struct blk_desc *crux_dev(void)
{
	struct blk_desc *desc = blk_get_devnum_by_uclass_id(UCLASS_SCSI, 0);

	return desc && desc->blksz == CRUX_BLKSZ ? desc : NULL;
}

static int crux_layout(struct blk_desc *desc)
{
	struct disk_partition info;

	if (part_get_info_by_name(desc, "pesystem", &info) < 0 ||
	    info.start != CRUX_PESYSTEM_LBA || info.size != CRUX_PESYSTEM_BLKS)
		return -ENODEV;
	if (part_get_info_by_name(desc, "cache", &info) < 0 ||
	    info.start != CRUX_CACHE_LBA || info.size != CRUX_CACHE_BLKS)
		return -ENODEV;
	return 0;
}

static int crux_read_state(struct blk_desc *desc, char vals[][CRUX_VAL_MAX])
{
	ALLOC_CACHE_ALIGN_BUFFER(u8, blk, CRUX_BLKSZ);

	if (blk_dread(desc, CRUX_STATE_LBA, 1, blk) != 1)
		return -EIO;
	if (crux_parse(blk, crux_state_keys, S_COUNT, vals) ||
	    strcmp(vals[S_VER], "1") || !vals[S_TXN][0]) {
		memset(vals, 0, S_COUNT * CRUX_VAL_MAX);
		return -ENOENT;
	}
	return 0;
}

static int crux_write_state(struct blk_desc *desc, char vals[][CRUX_VAL_MAX])
{
	ALLOC_CACHE_ALIGN_BUFFER(u8, out, CRUX_BLKSZ);
	ALLOC_CACHE_ALIGN_BUFFER(u8, back, CRUX_BLKSZ);

	crux_build(out, crux_state_keys, S_COUNT, vals);
	if (blk_dwrite(desc, CRUX_STATE_LBA, 1, out) != 1 ||
	    blk_dread(desc, CRUX_STATE_LBA, 1, back) != 1 ||
	    memcmp(out, back, CRUX_BLKSZ)) {
		printf("crux-ota: state block write/readback failed\n");
		return -EIO;
	}
	return 0;
}

static int crux_read_request(struct blk_desc *desc, char vals[][CRUX_VAL_MAX])
{
	struct disk_partition info;
	loff_t size = 0, got = 0;
	int part, ret;
	u8 *buf;

	part = part_get_info_by_name(desc, "pemetadata", &info);
	if (part < 0)
		return part;
	if (fs_set_blk_dev_with_part(desc, part) || fs_size(CRUX_REQUEST, &size) ||
	    size != CRUX_BLKSZ)
		return -ENOENT;
	buf = memalign(ARCH_DMA_MINALIGN, CRUX_BLKSZ);
	if (!buf)
		return -ENOMEM;
	ret = fs_set_blk_dev_with_part(desc, part);
	if (!ret)
		ret = fs_read(CRUX_REQUEST, map_to_sysmem(buf), 0, CRUX_BLKSZ, &got);
	if (!ret && got != CRUX_BLKSZ)
		ret = -EIO;
	if (!ret)
		ret = crux_parse(buf, crux_request_keys, R_COUNT, vals);
	free(buf);
	if (!ret && (strcmp(vals[R_VER], "1") || !vals[R_TXN][0]))
		ret = -EINVAL;
	return ret;
}

static int crux_crc_range(struct blk_desc *desc, ulong lba, ulong blks, u32 *crc)
{
	u8 *buf = map_sysmem(CRUX_VERIFY_ADDR, CRUX_CHUNK_BLKS * CRUX_BLKSZ);
	u32 value = 0;

	while (blks) {
		ulong n = min(blks, CRUX_CHUNK_BLKS);

		if (blk_dread(desc, lba, n, buf) != n)
			return -EIO;
		value = crc32(value, buf, n * CRUX_BLKSZ);
		lba += n;
		blks -= n;
	}
	*crc = value;
	return 0;
}

/* CRCs of the six neighbor ranges followed by both PE windows. */
static int crux_snapshot(struct blk_desc *desc, u32 *crcs)
{
	int i, n = ARRAY_SIZE(crux_neighbors);

	for (i = 0; i < n; i++)
		if (crux_crc_range(desc, crux_neighbors[i].lba, crux_neighbors[i].blks, &crcs[i]))
			return -EIO;
	for (i = 0; i < ARRAY_SIZE(crux_windows); i++)
		if (crux_crc_range(desc, crux_windows[i].lba, crux_windows[i].blks, &crcs[n + i]))
			return -EIO;
	if (crcs[CRUX_TWRP_INDEX] != CRUX_TWRP_CRC) {
		printf("crux-ota: retained TWRP slot CRC %08x differs\n", crcs[CRUX_TWRP_INDEX]);
		return -EINVAL;
	}
	return 0;
}

static int crux_crc_value(const char *text, u32 *crc)
{
	char *end;

	if (strlen(text) != 8)
		return -EINVAL;
	*crc = hextoul(text, &end);
	return *end ? -EINVAL : 0;
}

/* Check one staged FIT before any cache write. */
static int crux_check_stage(struct blk_desc *desc, const struct crux_window *w, u32 want)
{
	ulong bytes = w->blks * CRUX_BLKSZ;
	u8 *buf = map_sysmem(CRUX_STAGE_ADDR, bytes);
	u32 crc;

	if (blk_dread(desc, w->stage_lba, w->blks, buf) != w->blks)
		return -EIO;
	crc = crc32(0, buf, bytes);
	if (crc != want) {
		printf("crux-ota: staged %s FIT CRC %08x, expected %08x\n", w->name, crc, want);
		return -EBADMSG;
	}
	if (fdt_magic(buf) != FDT_MAGIC || fdt_totalsize(buf) > bytes) {
		printf("crux-ota: staged %s image is not a FIT inside its window\n", w->name);
		return -EBADMSG;
	}
	return 0;
}

/* Returns 0, or -EAGAIN when nothing was written, or another error after a write. */
static int crux_land(struct blk_desc *desc, const bool *land, const u32 *want)
{
	int n = ARRAY_SIZE(crux_neighbors), i;
	u32 before[ARRAY_SIZE(crux_neighbors) + ARRAY_SIZE(crux_windows)];
	u32 after[ARRAY_SIZE(crux_neighbors) + ARRAY_SIZE(crux_windows)];
	bool written = false;

	if (crux_snapshot(desc, before))
		return -EAGAIN;
	for (i = 0; i < ARRAY_SIZE(crux_windows); i++) {
		const struct crux_window *w = &crux_windows[i];
		u32 crc;

		if (!land[i])
			continue;
		if (crux_check_stage(desc, w, want[i]))
			return written ? -EIO : -EAGAIN;
		printf("crux-ota: landing %s FIT at 0x%lx x 0x%lx\n", w->name, w->lba, w->blks);
		written = true;
		if (blk_dwrite(desc, w->lba, w->blks, map_sysmem(CRUX_STAGE_ADDR, 0)) != w->blks ||
		    crux_crc_range(desc, w->lba, w->blks, &crc) || crc != want[i]) {
			printf("crux-ota: %s window readback failed\n", w->name);
			return -EIO;
		}
	}
	if (crux_snapshot(desc, after))
		return -EIO;
	for (i = 0; i < n + ARRAY_SIZE(crux_windows); i++) {
		if (i >= n && land[i - n])
			continue;
		if (before[i] != after[i]) {
			printf("crux-ota: cache range %d changed (%08x -> %08x)\n", i, before[i], after[i]);
			return -EIO;
		}
	}
	return 0;
}

static void crux_action(const char *action, const char *txn)
{
	env_set("crux_ota_action", action);
	env_set("crux_ota_txn", txn);
}

static int crux_fail(struct blk_desc *desc, char s[][CRUX_VAL_MAX], const char *msg)
{
	strlcpy(s[S_STAGE], "failed", CRUX_VAL_MAX);
	strlcpy(s[S_MSG], msg, CRUX_VAL_MAX);
	crux_write_state(desc, s);
	printf("crux-ota: transaction %s stopped: %s\n", s[S_TXN], msg);
	crux_action("stop", s[S_TXN]);
	return CMD_RET_SUCCESS;
}

static int crux_check(void)
{
	char s[S_COUNT][CRUX_VAL_MAX], r[R_COUNT][CRUX_VAL_MAX];
	struct blk_desc *desc = crux_dev();
	bool have_state, requested, land[ARRAY_SIZE(crux_windows)] = { false, true };
	u32 want[ARRAY_SIZE(crux_windows)] = { 0 };
	const char *stage;
	char extra[80];
	int tries, ret;

	env_set("crux_ota_extra", NULL);
	crux_action("menu", NULL);
	if (!desc || crux_layout(desc)) {
		printf("crux-ota: LUN0 layout differs; no install handoff\n");
		return CMD_RET_SUCCESS;
	}
	have_state = !crux_read_state(desc, s);
	/* A landing transaction finishes before any request is considered. */
	requested = (!have_state || strcmp(s[S_STAGE], "land")) &&
		    !crux_read_request(desc, r) && !strcmp(r[R_ACTION], "install");
	if (requested && (!have_state || strcmp(s[S_TXN], r[R_TXN]))) {
		if (have_state)
			printf("crux-ota: request %s replaces %s (stage %s)\n",
			       r[R_TXN], s[S_TXN], s[S_STAGE]);
		memset(s, 0, sizeof(s));
		strlcpy(s[S_VER], "1", CRUX_VAL_MAX);
		strlcpy(s[S_TXN], r[R_TXN], CRUX_VAL_MAX);
		strlcpy(s[S_STAGE], "recovery", CRUX_VAL_MAX);
		printf("crux-ota: new install request %s\n", s[S_TXN]);
	} else if (!have_state || !strcmp(s[S_STAGE], "done") || !strcmp(s[S_STAGE], "failed") ||
		   (!strcmp(s[S_STAGE], "recovery") && !requested)) {
		/* Nothing pending, finished, or the ROM withdrew its request. */
		return CMD_RET_SUCCESS;
	}
	stage = s[S_STAGE];
	if (strcmp(stage, "recovery") && strcmp(stage, "land") && strcmp(stage, "abort")) {
		printf("crux-ota: transaction %s has unknown stage '%s'\n", s[S_TXN], stage);
		crux_action("stop", s[S_TXN]);
		return CMD_RET_SUCCESS;
	}
	if (!strcmp(stage, "abort")) {
		strlcpy(s[S_STAGE], "done", CRUX_VAL_MAX);
		if (crux_write_state(desc, s))
			return CMD_RET_SUCCESS;
		printf("crux-ota: transaction %s aborted before writes; booting PE ROM\n", s[S_TXN]);
		crux_action("rom", s[S_TXN]);
		return CMD_RET_SUCCESS;
	}
	tries = (int)dectoul(s[S_TRIES], NULL) + 1;
	if (tries > CRUX_MAX_TRIES) {
		printf("crux-ota: transaction %s stage %s already tried %d times\n",
		       s[S_TXN], stage, tries - 1);
		crux_action("stop", s[S_TXN]);
		return CMD_RET_SUCCESS;
	}
	snprintf(s[S_TRIES], CRUX_VAL_MAX, "%d", tries);
	if (crux_write_state(desc, s))
		return CMD_RET_SUCCESS;
	if (!strcmp(stage, "recovery")) {
		snprintf(extra, sizeof(extra), "androidboot.crux_ota_txn=%s", s[S_TXN]);
		env_set("crux_ota_extra", extra);
		printf("crux-ota: transaction %s: starting PE Recovery (try %d)\n", s[S_TXN], tries);
		crux_action("recovery", s[S_TXN]);
		return CMD_RET_SUCCESS;
	}
	land[0] = !strcmp(s[S_REC], "1");
	if (crux_crc_value(s[S_ROM_CRC], &want[1]) ||
	    (land[0] && crux_crc_value(s[S_REC_CRC], &want[0])))
		return crux_fail(desc, s, "bad-crc-field");
	ret = crux_land(desc, land, want);
	if (ret == -EAGAIN) {
		printf("crux-ota: transaction %s: nothing written (try %d)\n", s[S_TXN], tries);
		crux_action("stop", s[S_TXN]);
		return CMD_RET_SUCCESS;
	}
	if (ret)
		return crux_fail(desc, s, "landing-failed-after-write");
	strlcpy(s[S_STAGE], "done", CRUX_VAL_MAX);
	if (crux_write_state(desc, s))
		return CMD_RET_SUCCESS;
	printf("crux-ota: transaction %s landed; booting PE ROM\n", s[S_TXN]);
	crux_action("rom", s[S_TXN]);
	return CMD_RET_SUCCESS;
}

/* Index of the identity key a "key=value" token names, or -1. */
static int crux_identity_index(const char *token)
{
	size_t len = strcspn(token, "=");
	int i;

	for (i = 0; i < ARRAY_SIZE(crux_identity); i++)
		if (strlen(crux_identity[i]) == len && !strncmp(token, crux_identity[i], len))
			return token[len] == '=' ? i : -1;
	return -1;
}

static const char *crux_chosen_bootargs(const void *fdt)
{
	int node;

	if (!fdt || fdt_check_header(fdt))
		return NULL;
	node = fdt_path_offset(fdt, "/chosen");
	return node < 0 ? NULL : fdt_getprop(fdt, node, "bootargs", NULL);
}

/* Save the ABL identity tokens before any payload overwrites ABL's DT. */
static int crux_abl(void)
{
	ulong addr = env_get_hex("prevbl_fdt_addr", 0);
	const char *args = crux_chosen_bootargs(addr ? map_sysmem(addr, 0) : NULL);
	char *copy, *cursor, *token, *out;
	bool seen[ARRAY_SIZE(crux_identity)] = { false };
	int i;

	if (!args) {
		printf("crux-ota: no ABL bootargs at prevbl_fdt_addr\n");
		return CMD_RET_FAILURE;
	}
	copy = strdup(args);
	out = calloc(1, strlen(args) + 1);
	if (!copy || !out) {
		free(copy);
		free(out);
		return CMD_RET_FAILURE;
	}
	for (cursor = copy; (token = strsep(&cursor, " ")); ) {
		i = crux_identity_index(token);
		if (i < 0 || seen[i])
			continue;
		seen[i] = true;
		if (*out)
			strcat(out, " ");
		strcat(out, token);
	}
	env_set("crux_abl_args", *out ? out : NULL);
	free(copy);
	free(out);
	if (!seen[0]) {
		printf("crux-ota: ABL bootargs lack androidboot.serialno\n");
		return CMD_RET_FAILURE;
	}
	return CMD_RET_SUCCESS;
}

/*
 * Replace FIT identity tokens with the saved ABL ones, name this U-Boot role
 * (androidboot.crux_uboot) for installers, and set bootargs.
 */
static int crux_bootargs(void)
{
	const char *abl = env_get("crux_abl_args");
	const char *extra = env_get("crux_ota_extra");
	const char *role = env_get("crux_uboot_role");
	const char *dt = crux_chosen_bootargs(working_fdt);
	char *copy, *cursor, *token, *out;

	if (!dt || !abl || !*abl || !role || !crux_value_ok(role, strlen(role))) {
		printf("crux-ota: missing %s; not booting\n",
		       !dt ? "FIT bootargs" : !role ? "U-Boot role" : "ABL identity");
		return CMD_RET_FAILURE;
	}
	copy = strdup(dt);
	out = calloc(1, strlen(dt) + strlen(abl) + strlen(role) + (extra ? strlen(extra) : 0) + 48);
	if (!copy || !out) {
		free(copy);
		free(out);
		return CMD_RET_FAILURE;
	}
	for (cursor = copy; (token = strsep(&cursor, " ")); ) {
		if (!*token || crux_identity_index(token) >= 0)
			continue;
		if (*out)
			strcat(out, " ");
		strcat(out, token);
	}
	strcat(out, " ");
	strcat(out, abl);
	strcat(out, " androidboot.crux_uboot=");
	strcat(out, role);
	if (extra && *extra) {
		strcat(out, " ");
		strcat(out, extra);
	}
	env_set("bootargs", out);
	free(copy);
	free(out);
	return CMD_RET_SUCCESS;
}

/*
 * Locate the FIT that "fastboot boot" left in the 0xC0000000 buffer: either
 * the raw FIT, or the Android boot image the host fastboot tool wraps around
 * a non-boot-image file (the FIT is then its kernel at one page offset).
 */
static int crux_fitaddr(void)
{
	const u8 *base = map_sysmem(CRUX_STAGE_ADDR, 4096);
	ulong addr = CRUX_STAGE_ADDR;

	if (!memcmp(base, "ANDROID!", 8)) {
		u32 kernel = get_unaligned_le32(base + 8), page = get_unaligned_le32(base + 36);

		if (page < 2048 || page > 16384 || (page & (page - 1)) || !kernel) {
			printf("crux-ota: unexpected Android boot image header\n");
			return CMD_RET_FAILURE;
		}
		addr += page;
		base = map_sysmem(addr, 4096);
		if (fdt_magic(base) != FDT_MAGIC || fdt_totalsize(base) > kernel) {
			printf("crux-ota: boot image kernel is not a FIT\n");
			return CMD_RET_FAILURE;
		}
	} else if (fdt_magic(base) != FDT_MAGIC) {
		printf("crux-ota: no FIT in the fastboot buffer\n");
		return CMD_RET_FAILURE;
	}
	env_set_hex("crux_fit_addr", addr);
	return CMD_RET_SUCCESS;
}

static int do_crux_ota(struct cmd_tbl *cmdtp, int flag, int argc, char *const argv[])
{
	if (argc != 2)
		return CMD_RET_USAGE;
	if (!strcmp(argv[1], "check"))
		return crux_check();
	if (!strcmp(argv[1], "abl"))
		return crux_abl();
	if (!strcmp(argv[1], "bootargs"))
		return crux_bootargs();
	if (!strcmp(argv[1], "fitaddr"))
		return crux_fitaddr();
	return CMD_RET_USAGE;
}

U_BOOT_CMD(crux_ota, 2, 0, do_crux_ota,
	   "Crux PE install handoff (crux-install/1)",
	   "check    - act on the pesystem-tail state or the pemetadata request\n"
	   "crux_ota abl      - save the ABL identity bootargs\n"
	   "crux_ota bootargs - set bootargs from the working FIT DT and ABL identity\n"
	   "crux_ota fitaddr  - set crux_fit_addr to the FIT in the fastboot buffer");
