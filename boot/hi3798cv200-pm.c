/* SPDX-License-Identifier: GPL-2.0-only */
#include "hi3798cv200-pm.h"

#define SYS 0xf8000000UL
#define CRG 0xf8a22000UL
#define PMC 0xf8a23000UL
#define MCU 0xf8400000UL
#define DATA (MCU + 0x7000)
#define START (MCU + 0xf000)
#define GICD 0xf1001000UL
#define GICC 0xf1002000UL
#define SIP 0xc2003798U
#define OFF 1U
#define ON 0U
#define PENDING 2U
#define STOPPING 3U
#define EINVAL 22
#define EACCES 13
#define EBUSY 16
#define EIO 5
#define ENODATA 61
#define EOPNOTSUPP 95
#define ETIMEDOUT 110

static inline u32 rd(u64 a) { return *(volatile u32 *)a; }
static inline void barrier(void) { __asm__ volatile("dsb sy" ::: "memory"); }
static inline void wr(u64 a, u32 v) { *(volatile u32 *)a = v; barrier(); }
static inline u32 cpu(void) { u64 v; __asm__ volatile("mrs %0, mpidr_el1" : "=r"(v)); return v & 255; }
static inline void event(void) { __asm__ volatile("sev" ::: "memory"); }
static void fatal(void) __attribute__((noreturn));
static void fatal(void) { for (;;) __asm__ volatile("wfi" ::: "memory"); }

/* EL3 runs with MMU/cache off. Bakery locking deliberately avoids exclusive
 * accesses to default Device memory. Tickets live only in reserved monitor RAM.
 */
static volatile u32 choosing[4], ticket[4];
static volatile u32 state[4];
static u64 entry[4], context[4];
static u32 armed, owned, sleeping, wake_valid;
static u32 irlo, irhi, timeout, enabled;
static u32 ca_chip, ca_vendor;
static u32 wake[4];
static u32 saved_gen[11];
static u32 pwm[2], pll[7][2], pll_locks, cpu_clock, bus_clock, ddr_timer;
static u32 ir_access;
static u32 saved_svb;
static u32 gic_words, gic_ctlr, gicc[4], gic_sgi[4];
static u32 gic_group[32], gic_enable[32], gic_pending[32], gic_active[32];
static u32 gic_priority[256], gic_target[256], gic_config[64];
static const u32 pll_offset[7] = {0x00, 0x08, 0x10, 0x20, 0x28, 0x30, 0x38};
static const u32 c51_code[] = {
#include DVBIP_PM_C51_PATH
};

static void lock(void)
{
	u32 c = cpu(), max = 0;
	choosing[c] = 1; barrier();
	for (u32 n = 0; n < 4; n++) if (ticket[n] > max) max = ticket[n];
	ticket[c] = max + 1; barrier(); choosing[c] = 0; barrier();
	for (u32 n = 0; n < 4; n++) {
		while (choosing[n]) barrier();
		while (ticket[n] && (ticket[n] < ticket[c] ||
		       (ticket[n] == ticket[c] && n < c))) barrier();
	}
}
static void unlock(void) { ticket[cpu()] = 0; barrier(); }
static int valid_entry(u64 v)
{
	/* Entire board DRAM is 0..2GiB. Exclude monitor/mailbox reservation. */
	return v && !(v & 3) && v < 0x80000000UL &&
	       (v < 0x11000000UL || v >= 0x11010000UL);
}
static void delay_us(u32 us)
{
	u64 a, b;
	__asm__ volatile("mrs %0, cntpct_el0" : "=r"(a));
	do { __asm__ volatile("mrs %0, cntpct_el0" : "=r"(b)); }
	while (b - a < (u64)us * 24);
}
static int wait_mask(u64 a, u32 mask, u32 value)
{
	for (u32 n = 0; n < 10000; n++) {
		if ((rd(a) & mask) == value) return 0;
		delay_us(10);
	}
	return -ETIMEDOUT;
}
static int interrupt_pending(void)
{
	u64 pending;
	__asm__ volatile("mrs %0, isr_el1" : "=r"(pending));
	return pending != 0;
}
static int hardware_quiescent(void)
{
	/* CV200 CRG22 request and CRG85 acknowledgement must agree. */
	if (((rd(CRG + 0x154) >> 12) & 3) != (rd(CRG + 0x58) & 3)) return 0;
	/* Linux rebuilds its GIC CPU interface, not an interrupted priority
	 * stack. Never power down with a secure or non-secure APR to preserve. */
	for (u32 n = 0; n < 4; n++)
		if (rd(GICC + 0xd0 + n * 4) || rd(GICC + 0xe0 + n * 4)) return 0;
	return !interrupt_pending();
}
static int security(void)
{
	/* Read the BSP CA identity layout, never turn an unknown value into a
	 * non-CA development chip. No fuse, security lock or key register writes.
	 */
	u32 a = rd(0xf8ab0128), b = rd(0xf8ab0060);
	int legacy = a == 0x2a13c812 || a == 0x6edbe953;
	int nocs = b == 0x2a13c812 || b == 0x6edbe953;
	if ((!legacy && !nocs) || (legacy && nocs && a != b)) return -EACCES;
	ca_chip = (legacy ? a : b) == 0x6edbe953;
	ca_vendor = ca_chip ? rd(legacy ? 0xf8ab012c : 0xf8ab00f0) & 255 : 0;
	if (rd(legacy ? 0xf8a90144 : 0xf8ab4108) & 3) return -EACCES;
	if (rd(0xf8ab0080) & 0x10) return -EACCES; /* DDR wake disabled */
	/* A running authenticated runtime-check policy must not be silently
	 * replaced by newly invented vectors or by suppressing the check flag.
	 */
	if (rd(0xf8ab0084) & 0x100000) return -EOPNOTSUPP;
	/* NAGRA's C51 branch intentionally skips DDRPHY save/selfrefresh; its
	 * OEM preparation is not the generic CV200 path. Vendor7 may select
	 * VERIMATRIX_ULTRA's different image/TEE service; identity alone cannot
	 * authorize selecting the non-Ultra image.
	 */
	if (ca_chip) {
		switch (ca_vendor) {
		case 2: case 3: case 5: case 6: case 8: case 0xb: case 0xc: case 0xe:
			break;
		default: return -EOPNOTSUPP;
		}
	}
	return 0;
}
static int load_mcu(void)
{
	int rc = security();
	if (rc) return rc;
	if (rd(START)) return -EBUSY;
	for (u32 n = 0; n < sizeof(c51_code) / sizeof(c51_code[0]); n++)
		wr(MCU + n * 4, c51_code[n]);
	for (u32 n = 0; n < sizeof(c51_code) / sizeof(c51_code[0]); n++)
		if (rd(MCU + n * 4) != c51_code[n]) return -EIO;
	owned = 1;
	return 0;
}
/* BSP ddr_wakeup_check/main.c + hash.c protocol, with bounded waits and
 * checked errors. This is SHA1, not HMAC/key-ladder access. The five raw
 * digest-register words preserve exactly the BSP/BootROM byte convention.
 */
static int prepare_wake_hash(void)
{
	if (!ca_chip || !(rd(0xf8ab0084) & (1U << 24))) return 0;
	u64 h = 0xf9a10000;
	u32 old = rd(CRG + 0xc4), digest[5];
	if ((old & 1) && !(rd(h + 8) & 1)) return -EBUSY;
	pm_clean_all();
	wr(CRG + 0xc4, old | 0x10); delay_us(10);
	wr(CRG + 0xc4, (old & ~0x10U) | 1); delay_us(10);
	int rc = wait_mask(h + 8, 1, 1);
	if (rc) goto out;
	/* C51 SetDDRWakeUpParams fixes address0, length10MiB, count1. */
	wr(h, 0xa00040); wr(h + 4, 0);
	wr(h + 0xc, rd(h + 0xc) & ~7U); wr(h + 0x10, 1);
	rc = wait_mask(h + 8, 8, 8); if (rc) goto out;
	wr(h + 0x14, 0); wr(h + 0x18, 0xa00000);
	rc = wait_mask(h + 0x20, 0xffffffffU, 0xa00000); if (rc) goto out;
	wr(h + 0xc, rd(h + 0xc) | 1);
	for (u32 n = 0; n < 16; n++) {
		rc = wait_mask(h + 8, 8, 8); if (rc) goto out;
		wr(h + 0x1c, n == 0 ? 0x80 : n == 15 ? 5 : 0);
	}
	rc = wait_mask(h + 8, 1, 1); if (rc) goto out;
	if (rd(h + 8) & 0xf0) { rc = -EIO; goto out; }
	for (u32 n = 0; n < 5; n++) digest[n] = rd(h + 0x30 + n * 4);
	for (u32 n = 0; n < 5; n++) wr(SYS + 0xf6c + n * 4, digest[n]);
out:
	/* Reset only the engine acquired above, then restore its clock field. */
	wr(CRG + 0xc4, rd(CRG + 0xc4) | 0x10);
	wr(CRG + 0xc4, old);
	return rc;
}
static void restore_gen(void)
{
	wr(SYS + 0xa4, saved_gen[0]); wr(SYS + 0xf8, saved_gen[1]);
	wr(SYS + 0xb0, saved_gen[2]); wr(SYS + 0xb4, saved_gen[3]);
	wr(SYS + 0xbc, saved_gen[4]); wr(SYS + 0xcc, saved_gen[5]);
	for (u32 n = 0; n < 5; n++) wr(SYS + 0xf6c + n * 4, saved_gen[n + 6]);
	/* BootROM clears the GPU corner byte; Linux's selected OPP bin survives. */
	wr(SYS + 0xc4, saved_svb);
}
static void save_hardware(void)
{
	pwm[0] = rd(PMC + 0x18); pwm[1] = rd(PMC + 0x20);
	for (u32 n = 0; n < 7; n++) {
		pll[n][0] = rd(CRG + pll_offset[n]);
		pll[n][1] = rd(CRG + pll_offset[n] + 4);
	}
	/* CV200 pm_v100 reads the effective APLL integer divider. Its chip
	 * header also exposes the fractional status used by this snapshot. */
	u32 tuned = rd(CRG + 0x294) & 0xfff;
	if (tuned) {
		pll[0][1] = (pll[0][1] & ~0xfffU) | tuned;
		pll[0][0] = (pll[0][0] & ~0xffffffU) | (rd(CRG + 0x298) & 0xffffff);
	}
	pll_locks = rd(CRG + 0x150) & 0x7f;
	cpu_clock = rd(CRG + 0x48); bus_clock = rd(CRG + 0x58);
	ddr_timer = rd(CRG + 0x128);
	/* The existing cold handoff owns only these two IR access-policy bits. */
	ir_access = rd(0xf8a80010) & 0x60000U;
	saved_svb = rd(SYS + 0xc4);
	saved_gen[0] = rd(SYS + 0xa4); saved_gen[1] = rd(SYS + 0xf8);
	saved_gen[2] = rd(SYS + 0xb0); saved_gen[3] = rd(SYS + 0xb4);
	saved_gen[4] = rd(SYS + 0xbc); saved_gen[5] = rd(SYS + 0xcc);
	for (u32 n = 0; n < 5; n++) saved_gen[n + 6] = rd(SYS + 0xf6c + n * 4);

	gic_words = ((rd(GICD + 4) & 31) + 1);
	gic_ctlr = rd(GICD);
	gicc[0] = rd(GICC); gicc[1] = rd(GICC + 4); gicc[2] = rd(GICC + 8);
	gicc[3] = rd(GICC + 0x1c);
	for (u32 n = 0; n < 4; n++) gic_sgi[n] = rd(GICD + 0xf20 + n * 4);
	for (u32 n = 0; n < gic_words; n++) {
		gic_group[n] = rd(GICD + 0x80 + n * 4);
		gic_enable[n] = rd(GICD + 0x100 + n * 4);
		gic_pending[n] = rd(GICD + 0x200 + n * 4);
		gic_active[n] = rd(GICD + 0x300 + n * 4);
	}
	for (u32 n = 0; n < gic_words * 8; n++) {
		gic_priority[n] = rd(GICD + 0x400 + n * 4);
		gic_target[n] = rd(GICD + 0x800 + n * 4);
	}
	for (u32 n = 0; n < gic_words * 2; n++) gic_config[n] = rd(GICD + 0xc00 + n * 4);
}
static void restore_hardware(void)
{
	/* BootROM has already brought DDR back. Never touch its live DPLL. */
	if (rd(CRG + 0x10) != pll[2][0] || rd(CRG + 0x14) != pll[2][1]) fatal();
	/* Execute at BSP's 400MHz safe source while restoring voltage first. */
	wr(CRG + 0x48, 0x206); wr(CRG + 0x48, 0x606);
	/* Put the core bus on its safe source before lowering core voltage or
	 * touching source PLLs; a register write alone is not a mux ACK. */
	wr(CRG + 0x58, 0);
	if (wait_mask(CRG + 0x154, 3U << 12, 0)) fatal();
	wr(PMC + 0x18, pwm[0]); wr(PMC + 0x20, pwm[1]);
	if (rd(PMC + 0x18) != pwm[0] || rd(PMC + 0x20) != pwm[1]) fatal();
	/* Match CV200 c51_resume's voltage-settling interval. */
	delay_us(15000);
	for (u32 n = 0; n < 7; n++) {
		if (n == 2) continue;
		u64 a = CRG + pll_offset[n];
		if (rd(a) != pll[n][0] || rd(a + 4) != pll[n][1]) {
			wr(a + 4, pll[n][1] | (1U << 20));
			wr(a, pll[n][0]); wr(a + 4, pll[n][1]);
		}
	}
	if (wait_mask(CRG + 0x150, pll_locks, pll_locks)) fatal();
	wr(CRG + 0x128, ddr_timer);
	wr(CRG + 0x58, bus_clock);
	if (wait_mask(CRG + 0x154, 3U << 12, (bus_clock & 3U) << 12)) fatal();
	wr(CRG + 0x48, cpu_clock & ~(1U << 10)); wr(CRG + 0x48, cpu_clock);
	/* Restore the pre-sleep IR policy, not a blanket security unlock. */
	u32 access = rd(0xf8a80010);
	if ((access & 0x60000U) != ir_access) {
		wr(0xf8a80010, (access & ~0x60000U) | ir_access);
		if ((rd(0xf8a80010) & 0x60000U) != ir_access) fatal();
	}
	/* Preserve distributor configuration and pending wake events. ENTER
	 * required empty APR/NSAPR; Linux subsequently rebuilds its NS CPU
	 * interface and restores its own enable/active snapshot. */
	wr(GICD, 0);
	for (u32 n = 0; n < gic_words; n++) wr(GICD + 0x80 + n * 4, gic_group[n]);
	for (u32 n = 0; n < gic_words * 8; n++) {
		wr(GICD + 0x400 + n * 4, gic_priority[n]);
		if (n >= 8) wr(GICD + 0x800 + n * 4, gic_target[n]);
	}
	for (u32 n = 1; n < gic_words * 2; n++) wr(GICD + 0xc00 + n * 4, gic_config[n]);
	for (u32 n = 0; n < gic_words; n++) {
		wr(GICD + 0x100 + n * 4, gic_enable[n]);
		wr(GICD + 0x200 + n * 4, n ? gic_pending[n] : gic_pending[n] & 0xffff0000U);
		wr(GICD + 0x300 + n * 4, gic_active[n]);
	}
	for (u32 n = 0; n < 4; n++) wr(GICD + 0xf20 + n * 4, gic_sgi[n]);
	wr(GICC + 4, gicc[1]); wr(GICC + 8, gicc[2]);
	wr(GICC + 0x1c, gicc[3]);
	wr(GICC, gicc[0]); wr(GICD, gic_ctlr);
	restore_gen();
}
static void configure_mcu(void)
{
	/* CV200 chip enum0x21 + revision0x200. No invented CA vendor/key/GPIO.
	 * wdgon=0 means DDR retention, never reboot. dbgmask=1 is the BSP host
	 * default and does not suppress power-down (the suppression bit is4).
	 */
	wr(SYS + 0xb0, 0x02000021 | (ca_chip << 8));
	for (u32 n = 0; n < 16; n++) wr(DATA + n * 4, 0);
	wr(DATA, 0x02000021); wr(DATA + 8, 1);
	wr(DATA + 0x1c, (enabled & 2) ? timeout : 0xffffffffU);
	/* GPIO-key type suppresses unsupported keyled hardware accesses. */
	wr(DATA + 0x20, 5);
	wr(DATA + 0x28, ca_vendor);
	/* Raw mode's bounded match loop honors zero keys. The BSP standard
	 * ISR unconditionally compares slot0 even when ir_pmocnum is zero.
	 */
	wr(DATA + 0x30, 4); wr(DATA + 0x34, (enabled & 1) ? 1 : 0);
	wr(DATA + 0x38, irlo); wr(DATA + 0x3c, irhi);
	wr(DATA + 0x51c, 0); wr(DATA + 0x52c, 0);
	for (u32 n = 0; n < 4; n++) wr(DATA + 0xff0 + n * 4, 0xffffffffU);
	wr(SYS + 0xa4, (u64)pm_a32_bridge);
	wr(SYS + 0xf8, (u64)pm_warm_entry);
	/* CA ADVCA_RUN_CHECK consumes GEN15; normal WaitforEnterFlag GEN13. */
	wr(SYS + 0xbc, ca_chip ? 0x80510001 : saved_gen[4]);
	wr(SYS + 0xb4, ca_chip ? 0 : 0x12345678);
}
void pm_init(void)
{
	for (unsigned char *p = __pm_bss_start; p < __pm_bss_end; p++) *p = 0;
	for (u32 n = 0; n < 4; n++) state[n] = STOPPING;
	state[0] = ON;
	barrier();
}
void pm_secondary(void)
{
	u32 c = cpu();
	/* OFF is a secure-only WFE standby, not a claim of core power removal.
	 * Clean all private NS cache data and leave coherency before publishing
	 * OFF. EL3's MMU/cache are off, so subsequent monitor Device accesses
	 * cannot dirty a private cache. SEV can wake this secure park for CPU_ON;
	 * a masked WFI could not be released by SEV and would deadlock hotplug.
	 */
	pm_clean_private();
	u64 v;
	__asm__ volatile("mrs %0, S3_1_C15_C2_1" : "=r"(v));
	v &= ~0x40UL;
	__asm__ volatile("msr S3_1_C15_C2_1, %0\n\tdsb sy\n\tisb" :: "r"(v) : "memory");
	lock(); state[c] = OFF; unlock(); event();
	for (;;) {
		barrier();
		if (state[c] == PENDING) {
			lock();
			u64 e = entry[c], arg = context[c];
			state[c] = ON; unlock();
			pm_cpu_on_launch(e, arg);
		}
		__asm__ volatile("wfe" ::: "memory");
	}
}
void pm_warm(void)
{
	if (cpu() != 0 || sleeping != 0x4356504d || !owned) fatal();
	/* Read once before stopping/restarting C51: cause0 is valid IR, never a
	 * fabricated default. Poisoned/stale/invalid data remains unavailable.
	 */
	wake[0] = rd(DATA + 0xff8); wake[1] = rd(DATA + 0xffc);
	wake[2] = rd(DATA + 0xff0); wake[3] = rd(DATA + 0xff4);
	wake_valid = wake[0] <= 5 && wake[1] != 0xffffffffU;
	wr(START, 0);
	if (rd(START)) fatal();
	restore_hardware();
	/* CV200 BSP c51_resume clears the BootROM retention-resume marker. */
	wr(SYS + 0x80, 0);
	if (rd(SYS + 0x80)) fatal();
	sleeping = 0; armed = 0;
	/* MCU/system reset also stopped all secure secondary parks. Rearm them
	 * via the same cold EL3 secondary route, never release NS execution.
	 */
	wr(CRG + 0x50, rd(CRG + 0x50) | 0xee00U);
	for (u32 n = 1; n < 4; n++) state[n] = STOPPING;
	barrier();
	wr(0xf8a80034, 0x11000000);
	wr(0xf8a80030, rd(0xf8a80030) | 15);
	u32 old = rd(CRG + 0x48);
	wr(CRG + 0x48, 0x206); wr(CRG + 0x48, 0x606);
	wr(CRG + 0x50, rd(CRG + 0x50) & ~0xee00U);
	wr(CRG + 0x48, old & ~(1U << 10)); wr(CRG + 0x48, old);
	for (u32 n = 1; n < 4; n++) {
		u32 tries = 10000;
		while (state[n] != OFF && tries--) delay_us(10);
		if (state[n] != OFF) fatal();
	}
	pm_launch(entry[0], context[0]);
}
static int feature(u32 fid)
{
	switch (fid) {
	case 0x84000000: case 0x84000002:
	case 0x84000003: case 0xc4000003:
	case 0x84000004: case 0xc4000004:
	case 0x8400000a: case 0x80000000:
		return 0;
	default: return -1;
	}
}
void pm_dispatch(struct pm_frame *f)
{
	u64 a[6];
	for (u32 n = 0; n < 6; n++) a[n] = f->x[n];
	f->x[0] = -1UL;
	u32 fid = a[0], c = cpu();
	if (a[0] != fid) return;
	if (fid == 0x80000000) { f->x[0] = 0x10002; return; }
	if (fid == 0x84000000) { f->x[0] = 0x10000; return; }
	if (fid == 0x8400000a) { f->x[0] = feature(a[1]); return; }
	if (fid == 0x84000003 || fid == 0xc4000003) {
		if (fid == 0x84000003) { a[1] = (u32)a[1]; a[2] = (u32)a[2]; a[3] = (u32)a[3]; }
		if (a[1] >= 4 || !valid_entry(a[2])) { f->x[0] = -2UL; return; }
		for (u32 n = 0; state[a[1]] == STOPPING && n < 10000; n++) delay_us(10);
		lock();
		u32 target = a[1];
		if (state[target] == ON) f->x[0] = -4UL;
		else if (state[target] != OFF) f->x[0] = -5UL;
		else if (sleeping) f->x[0] = -3UL;
		else {
			entry[target] = a[2]; context[target] = a[3];
			state[target] = PENDING; f->x[0] = 0;
		}
		unlock(); event(); return;
	}
	if (fid == 0x84000004 || fid == 0xc4000004) {
		if (fid == 0x84000004) { a[1] = (u32)a[1]; a[2] = (u32)a[2]; }
		if (a[1] >= 4 || a[2]) { f->x[0] = -2UL; return; }
		lock(); f->x[0] = state[a[1]] == STOPPING ? PENDING : state[a[1]]; unlock(); return;
	}
	if (fid == 0x84000002) {
		lock(); state[c] = STOPPING; unlock();
		pm_secondary();
	}
	if (fid != SIP) return;
	for (u32 n = 0; n < 18; n++) f->x[n] = 0;
	if (a[1] == 0) { f->x[0] = 0x4356504d; f->x[1] = 1; f->x[2] = 3; return; }
	/* Linux prepares and unwinds before/after secondary CPU offlining.
	 * CONFIG, LAST_WAKE and CANCEL use only lock-protected global state;
	 * only ENTER depends on CPU0's saved execution and hardware context.
	 */
	if (c && a[1] == 2) { f->x[0] = -(u64)EINVAL; return; }
	lock();
	switch (a[1]) {
	case 1:
		armed = 0; wake_valid = 0;
		if (a[2] > 0xffffffffUL || a[3] > 0xffffffffUL || a[4] > 0xffffffffUL ||
		    !a[5] || (a[5] & ~3UL) ||
		    ((a[5] & 2) ? (!a[4] || a[4] == 0xffffffffUL) : a[4] != 0xffffffffUL)) {
			f->x[0] = -(u64)EINVAL; break;
		}
		f->x[0] = (u64)(long)load_mcu();
		if (!f->x[0]) {
			irlo = a[2]; irhi = a[3]; timeout = a[4]; enabled = a[5]; armed = 1;
			f->x[1] = 0x1ae4b668; /* first32 SHA256 of exact LE C51 image */
		}
		break;
	case 2:
		if (!valid_entry(a[2]) || a[3]) { armed = 0; f->x[0] = -(u64)EINVAL; break; }
		if (!armed || !owned) { f->x[0] = -(u64)EACCES; break; }
		armed = 0;
		f->x[0] = (u64)(long)security();
		if (f->x[0]) { armed = 0; break; }
		if (rd(START) || state[1] != OFF || state[2] != OFF || state[3] != OFF) {
			f->x[0] = -(u64)EBUSY; armed = 0; break;
		}
		if (!hardware_quiescent()) { f->x[0] = -(u64)EBUSY; break; }
		entry[0] = a[2]; context[0] = a[3];
		save_hardware();
		f->x[0] = (u64)(long)prepare_wake_hash();
		if (f->x[0]) { restore_gen(); break; }
		/* Hash preparation can take time. Recheck late wake interrupts as
		 * BSP PSCI does before entering an irreversible power transition. */
		if (interrupt_pending()) {
			restore_gen(); f->x[0] = -(u64)EBUSY; break;
		}
		configure_mcu(); sleeping = 0x4356504d;
		unlock();
		pm_deep_wait();
		/* Only an actual MCU-start readback failure returns normally. */
		lock(); sleeping = 0; restore_gen(); f->x[0] = -(u64)EIO;
		break;
	case 3:
		if (!wake_valid) f->x[0] = -(u64)ENODATA;
		else {
			f->x[0] = 0;
			for (u32 n = 0; n < 4; n++) f->x[n + 1] = wake[n];
			wake_valid = 0;
		}
		break;
	case 4:
		armed = 0; wake_valid = 0;
		if (owned) {
			wr(START, 0);
			f->x[0] = rd(START) ? -(u64)EIO : 0;
			if (!f->x[0]) owned = 0;
		} else f->x[0] = 0;
		break;
	default: f->x[0] = -(u64)EINVAL; break;
	}
	unlock();
}
