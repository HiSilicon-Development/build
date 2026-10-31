/* SPDX-License-Identifier: GPL-2.0-only */
/* Shared HiSTB AArch64 handoff; HISTB_HI3798MV310 selects MV310 policy. */

	.arch armv8-a
	.section .text.start, "ax", %progbits
	.global _start
	.type _start, %function

	.equ	SMP_FIRMWARE_BASE, 0x11000000
	.equ	SMP_RELEASE_ADDR, 0x1100f000
	.equ	PERI_CPU_AARCH_MODE, 0xf8a80030
	.equ	PERI_CPU_RVBARADDR, 0xf8a80034
	.equ	CPU_LP_CTRL, 0xf8a22048
	.equ	CPU_RESET_CTRL, 0xf8a22050
	.equ	CRG_BASE, 0xf8a22000
	.equ	APLL_TUNE_INT, 0x1a4
	.equ	APLL_TUNE_FRAC, 0x1a8
	.equ	APLL_TUNE_STEP_INT, 0x1ac
	.equ	APLL_TUNE_STEP_FRAC, 0x1b0
	.equ	APLL_TUNE_CTRL, 0x1b4
	.equ	APLL_TUNE_STATUS, 0x294
	.equ	CPU_PWM, 0xf8a23018
	.equ	SYS_COUNTER_CTRL, 0xf8bb0000
	.equ	SYS_COUNTER_ENABLE, 0xf8bb0020

_start:
	/* Establish a masked exception state before touching inherited state. */
	msr	daifset, #0xf
	ic	iallu
	dsb	sy
	isb
	mrs	x9, CurrentEL
#ifdef HISTB_CV200_DEEP_PM
	/* A resident monitor cannot be installed from a non-secure EL. */
	cmp	x9, #0xc
	b.ne	failed
#endif
	mrs	x10, mpidr_el1
	and	x10, x10, #0xff
	cbnz	x10, secondary_entry
	cmp	x9, #0xc
	b.eq	from_el3
	cmp	x9, #0x8
	b.eq	from_el2
	cmp	x9, #0x4
	b.eq	from_el1
	b	failed

from_el3:
#ifdef HISTB_CV200_DEEP_PM
	/* Resident EL3 owns SMC vectors and secure per-CPU stacks on CV200. */
	bl	pm_install
#endif
	adr	x0, marker_el3
	bl	puts
#ifndef HISTB_HI3798MV310
    /*
     * CV200 exposes the IR block to non-secure Linux only after this
     * attribute word is programmed.  Do not copy the broader DDRC/MISC/
     * TZASC lock writes: those make this warm-reset handoff hang.
     */
    ldr x9, =0xf8a80010        /* SEC_ATTR_CTRL_0_REG */
    ldr w10, =0xfff9ffff
    str w10, [x9]
    dsb sy
    isb
#endif

	/* Cortex-A53 requires SMPEN even when Linux starts only CPU0. */
	mrs	x0, S3_1_C15_C2_1
	orr	x0, x0, #0x40
	msr	S3_1_C15_C2_1, x0
	dsb	sy
	isb
	msr	cntps_ctl_el1, xzr
	bl	disable_inherited_timers

	#ifdef DVBIP_IP1002_PRE_SMP_APLL_1200
	bl	normalize_cpu_apll_1200
	#endif

	/* Match the vendor AArch64 boot path before entering non-secure EL2. */
	ldr	x0, =0xf1001000
	bl	gicv2_init_secure
	ldr	x0, =0xf1001000
	ldr	x1, =0xf1002000
	bl	gicv2_init_secure_percpu
	bl	power_up_secondaries
	adr	x0, marker_gic
	bl	puts
	adr	x0, marker_smp
	bl	puts

	/* Match the vendor non-secure AArch64 handoff policy. */
#ifdef HISTB_CV200_DEEP_PM
	mov	x0, #0x531 /* SCR.SMD=0: resident monitor accepts SMC. */
#else
	mov	x0, #0x5b1
#endif
	msr	scr_el3, x0
	msr	cptr_el3, xzr
	ldr	x0, =24000000
	msr	cntfrq_el0, x0
	/* Preserve SCTLR_EL2 RES1 with translation and caches disabled. */
	ldr	x0, =0x30c50830
	msr	sctlr_el2, x0
	mov	x0, #0x33ff
	msr	cptr_el2, x0
	mov	x0, #0x3c9
	msr	spsr_el3, x0
	adr	x0, from_el2
	msr	elr_el3, x0
	eret

from_el2:
	adr	x0, marker_el2
	bl	puts
	bl	disable_inherited_timers
	mrs	x0, cnthctl_el2
	orr	x0, x0, #3
	msr	cnthctl_el2, x0
	msr	cntvoff_el2, xzr
	msr	hcr_el2, xzr
	mrs	x0, sctlr_el2
	bic	x0, x0, #1
	bic	x0, x0, #4
	bic	x0, x0, #0x1000
	msr	sctlr_el2, x0
	b	enter_linux

secondary_entry:
	cmp	x9, #0xc
	b.eq	secondary_from_el3
	cmp	x9, #0x8
	b.eq	secondary_from_el2
	b	failed

secondary_from_el3:
#ifdef HISTB_CV200_DEEP_PM
	/* Never run cold GIC clearing or spin-table code after deep wake. */
	b	pm_secondary_entry
#endif
	/* Each Cortex-A53 must join the inner-shareable coherency domain. */
	mrs	x0, S3_1_C15_C2_1
	orr	x0, x0, #0x40
	msr	S3_1_C15_C2_1, x0
	dsb	sy
	isb
	msr	cntps_ctl_el1, xzr
	bl	disable_inherited_timers

	ldr	x0, =0xf1001000
	ldr	x1, =0xf1002000
	bl	gicv2_init_secure_percpu

	mov	x0, #0x5b1
	msr	scr_el3, x0
	msr	cptr_el3, xzr
	ldr	x0, =24000000
	msr	cntfrq_el0, x0
	ldr	x0, =0x30c50830
	msr	sctlr_el2, x0
	mov	x0, #0x33ff
	msr	cptr_el2, x0
	mov	x0, #0x3c9
	msr	spsr_el3, x0
	adr	x0, secondary_from_el2
	msr	elr_el3, x0
	eret

secondary_from_el2:
	bl	disable_inherited_timers
	mrs	x0, cnthctl_el2
	orr	x0, x0, #3
	msr	cnthctl_el2, x0
	msr	cntvoff_el2, xzr
	msr	hcr_el2, xzr
	mrs	x0, sctlr_el2
	bic	x0, x0, #1
	bic	x0, x0, #4
	bic	x0, x0, #0x1000
	msr	sctlr_el2, x0
	dsb	sy
	isb

	/* Linux writes secondary_holding_pen here and wakes us with SEV. */
	ldr	x20, =SMP_RELEASE_ADDR
1:
secondary_wait:
	ldr	x4, [x20]
	cbnz	x4, 2f
	wfe
	b	1b
2:
	mov	x0, xzr
	mov	x1, xzr
	mov	x2, xzr
	mov	x3, xzr
	br	x4

/* Warm reset does not guarantee that the previous AArch32 timer state died. */
disable_inherited_timers:
	msr	cntp_ctl_el0, xzr
	msr	cntv_ctl_el0, xzr
	msr	cnthp_ctl_el2, xzr
	isb
	ret

	#ifdef DVBIP_IP1002_PRE_SMP_APLL_1200
/*
 * Normalize the IP1002 Fastboot APLL state from 1596 MHz to 1200 MHz.
 * CPU0 is still alone at EL3 with DAIF masked, and selector 0 remains active
 * throughout so Linux may subsequently retune the same APLL through cpufreq.
 */
normalize_cpu_apll_1200:
	mov	x14, x30
	ldr	x9, =CRG_BASE
	ldr	w22, [x9, #0x48]
	ldr	w23, [x9, #0x0]
	ldr	w24, [x9, #0x4]
	ldr	w25, [x9, #APLL_TUNE_INT]
	ldr	w26, [x9, #APLL_TUNE_FRAC]
	ldr	w27, [x9, #APLL_TUNE_STEP_INT]
	ldr	w28, [x9, #APLL_TUNE_STEP_FRAC]
	ldr	w20, [x9, #APLL_TUNE_CTRL]
	ldr	w19, [x9, #APLL_TUNE_STATUS]

	adr	x0, marker_apll_normalize_before
	bl	puts
	mov	w0, w22
	bl	puthex32
	adr	x0, marker_apll_normalize_cfg0
	bl	puts
	mov	w0, w23
	bl	puthex32
	adr	x0, marker_apll_normalize_cfg1
	bl	puts
	mov	w0, w24
	bl	puthex32
	adr	x0, marker_apll_normalize_ctrl
	bl	puts
	mov	w0, w20
	bl	puthex32
	adr	x0, marker_apll_normalize_status
	bl	puts
	mov	w0, w19
	bl	puthex32
	adr	x0, marker_apll_normalize_pwm
	bl	puts
	ldr	x0, =CPU_PWM
	ldr	w0, [x0]
	bl	puthex32
	adr	x0, marker_newline
	bl	puts

	/* Every mismatch before the first store is a strict zero-write halt. */
	mov	w10, #0x600
	cmp	w22, w10
	b.ne	apll_initial_mismatch
	ldr	w10, =0x12000000
	cmp	w23, w10
	b.ne	apll_initial_mismatch
	ldr	w10, =0x0800210a
	cmp	w24, w10
	b.ne	apll_initial_mismatch
	cmp	w20, #1
	b.ne	apll_initial_mismatch
	cbnz	w19, apll_initial_mismatch
	ldr	x10, =SYS_COUNTER_CTRL
	ldr	w11, [x10]
	cmp	w11, #0x103
	b.ne	apll_initial_mismatch
	ldr	x10, =SYS_COUNTER_ENABLE
	ldr	w11, [x10]
	cmp	w11, #1
	b.ne	apll_initial_mismatch

	/* Re-read all writable state to reject an unstable inherited handoff. */
	ldr	w10, [x9, #0x48]
	cmp	w10, w22
	b.ne	apll_initial_mismatch
	ldr	w10, [x9, #0x0]
	cmp	w10, w23
	b.ne	apll_initial_mismatch
	ldr	w10, [x9, #0x4]
	cmp	w10, w24
	b.ne	apll_initial_mismatch
	ldr	w10, [x9, #APLL_TUNE_INT]
	cmp	w10, w25
	b.ne	apll_initial_mismatch
	ldr	w10, [x9, #APLL_TUNE_FRAC]
	cmp	w10, w26
	b.ne	apll_initial_mismatch
	ldr	w10, [x9, #APLL_TUNE_STEP_INT]
	cmp	w10, w27
	b.ne	apll_initial_mismatch
	ldr	w10, [x9, #APLL_TUNE_STEP_FRAC]
	cmp	w10, w28
	b.ne	apll_initial_mismatch
	ldr	w10, [x9, #APLL_TUNE_CTRL]
	cmp	w10, w20
	b.ne	apll_initial_mismatch
	ldr	w10, [x9, #APLL_TUNE_STATUS]
	cmp	w10, w19
	b.ne	apll_initial_mismatch

	/* 1200 MHz * postdiv1(2) * postdiv2(1) * refdiv(2) / 24 MHz. */
	lsr	w10, w23, #24
	and	w10, w10, #0x7
	lsr	w11, w23, #28
	and	w11, w11, #0x7
	lsr	w12, w24, #12
	and	w12, w12, #0x3f
	mul	w13, w10, w11
	mul	w13, w13, w12
	mov	w10, #50
	mul	w13, w13, w10
	cmp	w13, #0xc8
	b.ne	apll_initial_mismatch

	/* Program only documented fields and verify every pre-trigger store. */
	mov	w16, #0xfff
	bic	w10, w25, w16
	orr	w10, w10, w13
	str	w10, [x9, #APLL_TUNE_INT]
	dsb	sy
	ldr	w11, [x9, #APLL_TUNE_INT]
	cmp	w11, w10
	b.ne	apll_pretrigger_fail

	ldr	w16, =0x00ffffff
	bic	w10, w26, w16
	str	w10, [x9, #APLL_TUNE_FRAC]
	dsb	sy
	ldr	w11, [x9, #APLL_TUNE_FRAC]
	cmp	w11, w10
	b.ne	apll_pretrigger_fail

	mov	w16, #0xfff
	bic	w10, w27, w16
	orr	w10, w10, #1
	str	w10, [x9, #APLL_TUNE_STEP_INT]
	dsb	sy
	ldr	w11, [x9, #APLL_TUNE_STEP_INT]
	cmp	w11, w10
	b.ne	apll_pretrigger_fail

	ldr	w16, =0x00ffffff
	bic	w10, w28, w16
	str	w10, [x9, #APLL_TUNE_STEP_FRAC]
	dsb	sy
	ldr	w11, [x9, #APLL_TUNE_STEP_FRAC]
	cmp	w11, w10
	b.ne	apll_pretrigger_fail

	orr	w10, w20, #0x4
	bic	w10, w10, #0x8
	str	w10, [x9, #APLL_TUNE_CTRL]
	dsb	sy
	ldr	w11, [x9, #APLL_TUNE_CTRL]
	cmp	w11, w10
	b.ne	apll_pretrigger_fail
	ldr	w11, [x9, #APLL_TUNE_STATUS]
	cbnz	w11, apll_pretrigger_fail

	/* A32 configured the physical counter at 24 MHz: 2.4M ticks is 100 ms. */
	orr	w10, w10, #0x8
	str	w10, [x9, #APLL_TUNE_CTRL]
	dsb	sy
	mrs	x17, cntpct_el0
	ldr	x18, =2400000
	add	x17, x17, x18
apll_target_poll:
	ldr	w15, [x9, #APLL_TUNE_STATUS]
	tbnz	w15, #13, apll_target_wait
	and	w16, w15, #0xfff
	cmp	w16, w13
	b.eq	apll_target_reached
apll_target_wait:
	mrs	x18, cntpct_el0
	cmp	x18, x17
	b.lo	apll_target_poll
	b	apll_trigger_fail

apll_target_reached:
	/* The completed transaction must retain its exact programmed image. */
	mov	w16, #0xfff
	bic	w10, w25, w16
	orr	w10, w10, w13
	ldr	w11, [x9, #APLL_TUNE_INT]
	cmp	w11, w10
	b.ne	apll_trigger_fail
	ldr	w16, =0x00ffffff
	bic	w10, w26, w16
	ldr	w11, [x9, #APLL_TUNE_FRAC]
	cmp	w11, w10
	b.ne	apll_trigger_fail
	mov	w16, #0xfff
	bic	w10, w27, w16
	orr	w10, w10, #1
	ldr	w11, [x9, #APLL_TUNE_STEP_INT]
	cmp	w11, w10
	b.ne	apll_trigger_fail
	ldr	w16, =0x00ffffff
	bic	w10, w28, w16
	ldr	w11, [x9, #APLL_TUNE_STEP_FRAC]
	cmp	w11, w10
	b.ne	apll_trigger_fail
	orr	w10, w20, #0xc
	ldr	w11, [x9, #APLL_TUNE_CTRL]
	cmp	w11, w10
	b.ne	apll_trigger_fail
	ldr	w15, [x9, #APLL_TUNE_STATUS]
	tbnz	w15, #13, apll_trigger_fail
	and	w16, w15, #0xfff
	cmp	w16, w13
	b.ne	apll_trigger_fail
	ldr	w10, [x9, #0x48]
	cmp	w10, w22
	b.ne	apll_trigger_fail
	ldr	w10, [x9, #0x0]
	cmp	w10, w23
	b.ne	apll_trigger_fail
	ldr	w10, [x9, #0x4]
	cmp	w10, w24
	b.ne	apll_trigger_fail
	dsb	sy
	isb
	adr	x0, marker_apll_normalize_done
	bl	puts
	mov	w0, w15
	bl	puthex32
	adr	x0, marker_apll_normalize_rate
	bl	puts
	br	x14

apll_initial_mismatch:
	adr	x0, marker_apll_initial_fail
	bl	puts
	b	apll_normalize_halt

apll_pretrigger_fail:
	adr	x0, marker_apll_pretrigger_fail
	bl	puts
	/* Configuration fields are not live until tune_en rises. */
	str	w25, [x9, #APLL_TUNE_INT]
	str	w26, [x9, #APLL_TUNE_FRAC]
	str	w27, [x9, #APLL_TUNE_STEP_INT]
	str	w28, [x9, #APLL_TUNE_STEP_FRAC]
	str	w20, [x9, #APLL_TUNE_CTRL]
	dsb	sy
	ldr	w10, [x9, #APLL_TUNE_INT]
	cmp	w10, w25
	b.ne	apll_rollback_unverified
	ldr	w10, [x9, #APLL_TUNE_FRAC]
	cmp	w10, w26
	b.ne	apll_rollback_unverified
	ldr	w10, [x9, #APLL_TUNE_STEP_INT]
	cmp	w10, w27
	b.ne	apll_rollback_unverified
	ldr	w10, [x9, #APLL_TUNE_STEP_FRAC]
	cmp	w10, w28
	b.ne	apll_rollback_unverified
	ldr	w10, [x9, #APLL_TUNE_CTRL]
	cmp	w10, w20
	b.ne	apll_rollback_unverified
	ldr	w10, [x9, #APLL_TUNE_STATUS]
	cbnz	w10, apll_rollback_unverified
	ldr	w10, [x9, #0x48]
	cmp	w10, w22
	b.ne	apll_rollback_unverified
	ldr	w10, [x9, #0x0]
	cmp	w10, w23
	b.ne	apll_rollback_unverified
	ldr	w10, [x9, #0x4]
	cmp	w10, w24
	b.ne	apll_rollback_unverified
	adr	x0, marker_apll_rollback_verified
	bl	puts
	b	apll_normalize_halt

apll_trigger_fail:
	adr	x0, marker_apll_trigger_fail
	bl	puts
	mov	w0, w15
	bl	puthex32
	adr	x0, marker_newline
	bl	puts
	/* Never write a second transaction while the hardware still reports busy. */
	tbnz	w15, #13, apll_rollback_unverified
	ldr	w10, [x9, #0x48]
	cmp	w10, w22
	b.ne	apll_rollback_unverified
	ldr	w10, [x9, #0x0]
	cmp	w10, w23
	b.ne	apll_rollback_unverified
	ldr	w10, [x9, #0x4]
	cmp	w10, w24
	b.ne	apll_rollback_unverified

	/* Retune to the inherited 266 result, then restore the exact register image. */
	and	w13, w24, #0xfff
	mov	w16, #0xfff
	bic	w10, w25, w16
	orr	w10, w10, w13
	str	w10, [x9, #APLL_TUNE_INT]
	ldr	w16, =0x00ffffff
	bic	w10, w26, w16
	str	w10, [x9, #APLL_TUNE_FRAC]
	mov	w16, #0xfff
	bic	w10, w27, w16
	orr	w10, w10, #1
	str	w10, [x9, #APLL_TUNE_STEP_INT]
	ldr	w16, =0x00ffffff
	bic	w10, w28, w16
	str	w10, [x9, #APLL_TUNE_STEP_FRAC]
	orr	w10, w20, #0x4
	bic	w10, w10, #0x8
	str	w10, [x9, #APLL_TUNE_CTRL]
	dsb	sy

	/* A rollback transaction is allowed only after all setup reads back. */
	mov	w16, #0xfff
	bic	w11, w25, w16
	orr	w11, w11, w13
	ldr	w12, [x9, #APLL_TUNE_INT]
	cmp	w12, w11
	b.ne	apll_rollback_unverified
	ldr	w16, =0x00ffffff
	bic	w11, w26, w16
	ldr	w12, [x9, #APLL_TUNE_FRAC]
	cmp	w12, w11
	b.ne	apll_rollback_unverified
	mov	w16, #0xfff
	bic	w11, w27, w16
	orr	w11, w11, #1
	ldr	w12, [x9, #APLL_TUNE_STEP_INT]
	cmp	w12, w11
	b.ne	apll_rollback_unverified
	ldr	w16, =0x00ffffff
	bic	w11, w28, w16
	ldr	w12, [x9, #APLL_TUNE_STEP_FRAC]
	cmp	w12, w11
	b.ne	apll_rollback_unverified
	ldr	w11, [x9, #APLL_TUNE_CTRL]
	cmp	w11, w10
	b.ne	apll_rollback_unverified
	ldr	w11, [x9, #APLL_TUNE_STATUS]
	tbnz	w11, #13, apll_rollback_unverified
	ldr	w11, [x9, #0x48]
	cmp	w11, w22
	b.ne	apll_rollback_unverified
	ldr	w11, [x9, #0x0]
	cmp	w11, w23
	b.ne	apll_rollback_unverified
	ldr	w11, [x9, #0x4]
	cmp	w11, w24
	b.ne	apll_rollback_unverified
	orr	w10, w10, #0x8
	str	w10, [x9, #APLL_TUNE_CTRL]
	dsb	sy
	mrs	x17, cntpct_el0
	ldr	x18, =2400000
	add	x17, x17, x18
apll_rollback_poll:
	ldr	w15, [x9, #APLL_TUNE_STATUS]
	tbnz	w15, #13, apll_rollback_wait
	and	w16, w15, #0xfff
	cmp	w16, w13
	b.eq	apll_rollback_result_ok
apll_rollback_wait:
	mrs	x18, cntpct_el0
	cmp	x18, x17
	b.lo	apll_rollback_poll
	b	apll_rollback_unverified

apll_rollback_result_ok:
	str	w25, [x9, #APLL_TUNE_INT]
	str	w26, [x9, #APLL_TUNE_FRAC]
	str	w27, [x9, #APLL_TUNE_STEP_INT]
	str	w28, [x9, #APLL_TUNE_STEP_FRAC]
	str	w20, [x9, #APLL_TUNE_CTRL]
	dsb	sy
	ldr	w10, [x9, #APLL_TUNE_INT]
	cmp	w10, w25
	b.ne	apll_rollback_unverified
	ldr	w10, [x9, #APLL_TUNE_FRAC]
	cmp	w10, w26
	b.ne	apll_rollback_unverified
	ldr	w10, [x9, #APLL_TUNE_STEP_INT]
	cmp	w10, w27
	b.ne	apll_rollback_unverified
	ldr	w10, [x9, #APLL_TUNE_STEP_FRAC]
	cmp	w10, w28
	b.ne	apll_rollback_unverified
	ldr	w10, [x9, #APLL_TUNE_CTRL]
	cmp	w10, w20
	b.ne	apll_rollback_unverified
	ldr	w10, [x9, #0x48]
	cmp	w10, w22
	b.ne	apll_rollback_unverified
	ldr	w10, [x9, #0x0]
	cmp	w10, w23
	b.ne	apll_rollback_unverified
	ldr	w10, [x9, #0x4]
	cmp	w10, w24
	b.ne	apll_rollback_unverified
	ldr	w10, [x9, #APLL_TUNE_STATUS]
	tbnz	w10, #13, apll_rollback_unverified
	and	w10, w10, #0xfff
	cmp	w10, w13
	b.ne	apll_rollback_unverified
	adr	x0, marker_apll_rollback_verified
	bl	puts
	b	apll_normalize_halt

apll_rollback_unverified:
	adr	x0, marker_apll_rollback_fail
	bl	puts
apll_normalize_halt:
1:
	wfi
	b	1b
	#endif

/* CV200 ATF CPU_ON; MV310 uses its BSP's exact temporary clock values. */
power_up_secondaries:
	ldr	x9, =SMP_RELEASE_ADDR
	str	xzr, [x9]
	dsb	sy

	ldr	x9, =PERI_CPU_AARCH_MODE
#ifdef HISTB_HI3798MV310
	ldr	w10, [x9]
	orr	w10, w10, #0xf
#else
	mov	w10, #0xf
#endif
	str	w10, [x9]
	ldr	x9, =PERI_CPU_RVBARADDR
	ldr	w10, =SMP_FIRMWARE_BASE
	str	w10, [x9]
	dsb	sy

	mov	w11, #1
3:
	ldr	x9, =CPU_LP_CTRL
	ldr	w12, [x9]
#ifdef HISTB_HI3798MV310
	/* MV310 BSP platsmp.c: clock selector 6, divider bit9 set. */
	mov	w13, #0x206
#else
	/* Preserve every inherited bit except the mux selector and commit bit. */
	mov	w16, #0x407
	bic	w13, w12, w16
	orr	w13, w13, #0x6
#endif
	str	w13, [x9]
	orr	w13, w13, #0x400
	str	w13, [x9]
	dsb	sy

	ldr	x10, =CPU_RESET_CTRL
	ldr	w13, [x10]
	mov	w14, #1
	add	w15, w11, #12
	lsl	w14, w14, w15
	bic	w13, w13, w14
	str	w13, [x10]
	dsb	sy

	ldr	w13, [x10]
	mov	w14, #1
	add	w15, w11, #8
	lsl	w14, w14, w15
	bic	w13, w13, w14
	str	w13, [x10]
	dsb	sy

	bic	w13, w12, #0x400
	str	w13, [x9]
	str	w12, [x9]
	dsb	sy

	add	w11, w11, #1
	cmp	w11, #4
	b.ne	3b
	sev
	ret

/* Hi3798CV200 vendor Fastboot AArch64 GICv2 handoff sequence. */
gicv2_init_secure:
	/* Quiesce inherited distributor state before Linux reprograms it. */
	str	wzr, [x0, #0x0]
	ldr	w9, [x0, #0x4]
	and	w10, w9, #0x1f
	cbz	w10, 2f
	add	x11, x0, #0x84
	add	x12, x0, #0x284
	add	x13, x0, #0x384
	mov	w9, #-1
1:
	str	w9, [x11], #4
	str	w9, [x12], #4
	str	w9, [x13], #4
	subs	w10, w10, #1
	b.ne	1b
2:
	mov	w9, #0x3
	str	w9, [x0, #0x0]
	dsb	sy
	ret

gicv2_init_secure_percpu:
	mov	w9, #-1
	str	w9, [x0, #0x80]
	str	w9, [x0, #0x280]
	str	w9, [x0, #0x380]
	mov	w9, #0x1
	str	w9, [x0, #0x100]
	str	wzr, [x1, #0xd0]
	str	wzr, [x1, #0xd4]
	str	wzr, [x1, #0xd8]
	str	wzr, [x1, #0xdc]
	str	wzr, [x1, #0xe0]
	str	wzr, [x1, #0xe4]
	str	wzr, [x1, #0xe8]
	str	wzr, [x1, #0xec]
	mov	w9, #0x1e7
	str	w9, [x1, #0x0]
	mov	w9, #0x80
	str	w9, [x1, #0x4]
	dsb	sy
	isb
	ret

from_el1:
	adr	x0, marker_el1
	bl	puts
	mrs	x0, sctlr_el1
	bic	x0, x0, #1
	bic	x0, x0, #4
	bic	x0, x0, #0x1000
	msr	sctlr_el1, x0

enter_linux:
	dsb	sy
	isb
	movz	x0, #0x1000, lsl #16
	mov	x1, xzr
	mov	x2, xzr
	mov	x3, xzr
	movz	x4, #0x0200, lsl #16
	br	x4

failed:
	adr	x0, marker_bad_el
	bl	puts
1:
	wfi
	b	1b

puts:
	ldrb	w1, [x0], #1
	cbz	w1, 3f
2:
	ldr	x2, =0xf8b00018
	ldr	w3, [x2]
	tbnz	w3, #5, 2b
	ldr	x2, =0xf8b00000
	strb	w1, [x2]
	b	puts
3:
	ret

#ifdef DVBIP_IP1002_PRE_SMP_APLL_1200
puthex32:
	mov	w4, #8
4:
	lsr	w1, w0, #28
	cmp	w1, #10
	add	w1, w1, #'0'
	b.lo	5f
	add	w1, w1, #('a' - '0' - 10)
5:
	ldr	x2, =0xf8b00018
	ldr	w3, [x2]
	tbnz	w3, #5, 5b
	ldr	x2, =0xf8b00000
	strb	w1, [x2]
	lsl	w0, w0, #4
	subs	w4, w4, #1
	b.ne	4b
	ret
#endif

marker_el3:
	.asciz	"[A64] entered at EL3; dropping to EL2\r\n"
marker_gic:
	.asciz	"[A64] GICv2 handed to non-secure Linux\r\n"
marker_smp:
#if defined(HISTB_CV200_DEEP_PM)
	.asciz	"[A64] CPU1-3 owned by resident EL3 PSCI monitor\r\n"
#else
	.asciz	"[A64] CPU1-3 released into spin-table\r\n"
#endif
marker_el2:
	.asciz	"[A64] entering Linux at EL2\r\n"
marker_el1:
	.asciz	"[A64] entering Linux at EL1\r\n"
marker_bad_el:
	.asciz	"[A64] unsupported exception level\r\n"
#ifdef DVBIP_IP1002_PRE_SMP_APLL_1200
marker_newline:
	.asciz	"\r\n"
marker_apll_normalize_before:
	.asciz	"[A64] IP1002 APLL normalize before CRG18=0x"
marker_apll_normalize_cfg0:
	.asciz	" CFG0=0x"
marker_apll_normalize_cfg1:
	.asciz	" CFG1=0x"
marker_apll_normalize_ctrl:
	.asciz	" CTRL=0x"
marker_apll_normalize_status:
	.asciz	" STATUS=0x"
marker_apll_normalize_pwm:
	.asciz	" CPU_PWM=0x"
marker_apll_normalize_done:
	.asciz	"[A64] IP1002 APLL normalize complete STATUS=0x"
marker_apll_normalize_rate:
	.asciz	" RATE_KHZ=1200000 selector=0\r\n"
marker_apll_initial_fail:
	.asciz	"[A64] IP1002 APLL initial-state mismatch; zero clock writes, halted\r\n"
marker_apll_pretrigger_fail:
	.asciz	"[A64] IP1002 APLL pre-trigger verify failed; restoring snapshot\r\n"
marker_apll_trigger_fail:
	.asciz	"[A64] IP1002 APLL transaction failed STATUS=0x"
marker_apll_rollback_verified:
	.asciz	"[A64] IP1002 APLL rollback verified; halted\r\n"
marker_apll_rollback_fail:
	.asciz	"[A64] IP1002 APLL rollback unverified; halted\r\n"
#endif

	.ltorg
	.size _start, . - _start

#ifdef HISTB_CV200_DEEP_PM
#ifdef HISTB_HI3798MV310
#error "CV200 deep PM is not supported on MV310"
#endif
#include "hi3798cv200-pm-monitor.S"
#endif
