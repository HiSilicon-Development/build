/* SPDX-License-Identifier: GPL-2.0-only */
/* Included by the Hi3798CV200 EL3 handoff when HISTB_CV200_DEEP_PM is enabled. */
.section .text.pm,"ax",%progbits
.balign 16
.global pm_install
pm_install:
	mov x16, x30
	bl pm_stack
	stp x19, x16, [sp, #-16]!
	adr x0, pm_vectors
	msr vbar_el3, x0
	isb
	bl pm_init
	ldp x19, x30, [sp], #16
	ret

pm_stack:
	/* Every reset/park route must retain the uncached little-endian EL3
	 * contract before its first stack access. Do not disable a live MMU. */
	mrs x10, sctlr_el3
	tbnz x10, #0, pm_fatal
	tbnz x10, #2, pm_fatal
	tbnz x10, #12, pm_fatal
	tbnz x10, #25, pm_fatal
	mrs x9, mpidr_el1
	and x9, x9, #0xff
	cmp x9, #4
	b.hs pm_fatal
	ldr x10, =__pm_stack_top
	sub x10, x10, x9, lsl #11
	msr spsel, #1
	mov sp, x10
	ret

.global pm_secondary_entry
pm_secondary_entry:
	bl pm_stack
	adr x0, pm_vectors
	msr vbar_el3, x0
	isb
	b pm_secondary

.global pm_warm_entry
pm_warm_entry:
	msr daifset, #0xf
	mrs x9, CurrentEL
	cmp x9, #0xc
	b.ne pm_fatal
	bl pm_stack
	adr x0, pm_vectors
	msr vbar_el3, x0
	mrs x0, S3_1_C15_C2_1
	orr x0, x0, #0x40
	msr S3_1_C15_C2_1, x0
	dsb sy
	isb
	b pm_warm

/* Cold CPU_ON and hotplug need banked Group1 delivery reopened after Linux
 * disabled its CPU interface. Only private GIC configuration is touched:
 * no distributor SPI write, pending/active clear, APR reset, or IRQ ack.
 * CPU0 deep wake restores firmware context before Linux's NS irqchip resume.
 * Keep x0(entry)/x1(context) intact until pm_launch consumes them.
 */
.global pm_cpu_on_launch
pm_cpu_on_launch:
	ldr x14, =0xf1001000
	mov w15, #-1
	str w15, [x14, #0x80]
	mov w15, #0xffff
	str w15, [x14, #0x100]
	ldr x14, =0xf1002000
	str wzr, [x14, #8]
	str wzr, [x14, #0x1c]
	mov w15, #0x80
	str w15, [x14, #4]
	mov w15, #0x1e7
	str w15, [x14]
	dsb sy
	isb
	b pm_launch

/* Every call starts a fresh non-secure EL2 context, never an inherited EL1
 * translation regime. Linux CPU_ON/cpu_resume both accept this boot mode. */
.global pm_launch
pm_launch:
	msr daifset, #0xf
	/* CPU_OFF discarded a C stack. Reusing it would leak stack each hotplug. */
	bl pm_stack
	msr elr_el3, x0
	mov x0, x1
	mov x1, xzr
	mov x2, xzr
	mov x3, xzr
	mov x9, #0x531
	msr scr_el3, x9
	msr cptr_el3, xzr
	ldr x9, =0x30c50830
	msr sctlr_el2, x9
	msr hcr_el2, xzr
	mov x9, #0x33ff
	msr cptr_el2, x9
	mov x9, #3
	msr cnthctl_el2, x9
	msr cntvoff_el2, xzr
	msr vttbr_el2, xzr
	msr cntp_ctl_el0, xzr
	msr cntv_ctl_el0, xzr
	msr cnthp_ctl_el2, xzr
	msr cntps_ctl_el1, xzr
	ldr x9, =24000000
	msr cntfrq_el0, x9
	mov x9, #0x3c9
	msr spsr_el3, x9
	mrs x9, S3_1_C15_C2_1
	orr x9, x9, #0x40
	msr S3_1_C15_C2_1, x9
	dsb sy
	isb
	mov x4, xzr
	mov x5, xzr
	mov x6, xzr
	mov x7, xzr
	mov x8, xzr
	mov x9, xzr
	mov x10, xzr
	mov x11, xzr
	mov x12, xzr
	mov x13, xzr
	mov x14, xzr
	mov x15, xzr
	mov x16, xzr
	mov x17, xzr
	mov x18, xzr
	mov x19, xzr
	mov x20, xzr
	mov x21, xzr
	mov x22, xzr
	mov x23, xzr
	mov x24, xzr
	mov x25, xzr
	mov x26, xzr
	mov x27, xzr
	mov x28, xzr
	mov x29, xzr
	mov x30, xzr
	eret

.global pm_clean_private
pm_clean_private:
	mov x12, #1
	b pm_clean_levels
.global pm_clean_all
pm_clean_all:
	mrs x12, clidr_el1
	ubfx x12, x12, #24, #3
pm_clean_levels:
	mrs x13, csselr_el1
	mov x10, #0
1:
	cmp x10, x12
	b.hs 5f
	mrs x0, clidr_el1
	add x1, x10, x10, lsl #1
	lsr x0, x0, x1
	and x0, x0, #7
	cmp x0, #2
	b.lo 4f
	lsl x11, x10, #1
	msr csselr_el1, x11
	isb
	mrs x0, ccsidr_el1
	and x1, x0, #7
	add x1, x1, #4
	ubfx x2, x0, #3, #10
	clz w3, w2
	ubfx x4, x0, #13, #15
	/* Complete the preceding level's writebacks before cleaning this one. */
	dsb sy
2:
	mov x5, x2
3:
	lsl w6, w5, w3
	lsl x7, x4, x1
	orr x6, x6, x7
	orr x6, x6, x11
	dc cisw, x6
	subs x5, x5, #1
	b.ge 3b
	subs x4, x4, #1
	b.ge 2b
4:
	add x10, x10, #1
	b 1b
5:
	msr csselr_el1, x13
	dsb sy
	isb
	ret

.global pm_deep_wait
pm_deep_wait:
	stp x19, x30, [sp, #-16]!
	bl pm_clean_all
	mrs x0, S3_1_C15_C2_1
	bic x0, x0, #0x40
	msr S3_1_C15_C2_1, x0
	dsb sy
	isb
	ldr x0, =0xf840f000
	mov w1, #1
	str w1, [x0]
	dsb sy
	ldr w2, [x0]
	cmp w2, #1
	b.ne 2f
1:
	wfi
	b 1b
2:
	str wzr, [x0]
	dsb sy
	ldr w2, [x0]
	cbnz w2, pm_fatal
	mrs x0, S3_1_C15_C2_1
	orr x0, x0, #0x40
	msr S3_1_C15_C2_1, x0
	dsb sy
	isb
	ldp x19, x30, [sp], #16
	ret

pm_fatal:
	msr daifset, #0xf
1:
	wfi
	b 1b

/* Full 2KiB EL3 vector table. Only lower A64 SMC is a supported exception. */
.balign 2048
.global pm_vectors
pm_vectors:
.rept 8
	b pm_fatal
	.balign 128
.endr
	b pm_smc
	.balign 128
.rept 7
	b pm_fatal
	.balign 128
.endr
.global pm_smc
pm_smc:
	sub sp, sp, #256
	stp x0, x1, [sp, #0]
	stp x2, x3, [sp, #16]
	stp x4, x5, [sp, #32]
	stp x6, x7, [sp, #48]
	stp x8, x9, [sp, #64]
	stp x10, x11, [sp, #80]
	stp x12, x13, [sp, #96]
	stp x14, x15, [sp, #112]
	stp x16, x17, [sp, #128]
	stp x18, x19, [sp, #144]
	stp x20, x21, [sp, #160]
	stp x22, x23, [sp, #176]
	stp x24, x25, [sp, #192]
	stp x26, x27, [sp, #208]
	stp x28, x29, [sp, #224]
	str x30, [sp, #240]
	mrs x0, esr_el3
	lsr x0, x0, #26
	cmp x0, #0x17
	b.ne pm_fatal
	mov x0, sp
	bl pm_dispatch
	ldp x0, x1, [sp, #0]
	ldp x2, x3, [sp, #16]
	ldp x4, x5, [sp, #32]
	ldp x6, x7, [sp, #48]
	ldp x8, x9, [sp, #64]
	ldp x10, x11, [sp, #80]
	ldp x12, x13, [sp, #96]
	ldp x14, x15, [sp, #112]
	ldp x16, x17, [sp, #128]
	ldp x18, x19, [sp, #144]
	ldp x20, x21, [sp, #160]
	ldp x22, x23, [sp, #176]
	ldp x24, x25, [sp, #192]
	ldp x26, x27, [sp, #208]
	ldp x28, x29, [sp, #224]
	ldr x30, [sp, #240]
	add sp, sp, #256
	eret

.section .rodata.pm_bridge,"a",%progbits
.balign 4
.global pm_a32_bridge
pm_a32_bridge:
.incbin DVBIP_PM_BRIDGE_PATH
