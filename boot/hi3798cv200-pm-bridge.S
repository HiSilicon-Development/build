/* SPDX-License-Identifier: GPL-2.0-only */
/* Compiled separately for ARMv7-A; BootROM has restored DDR via GEN16 here.
 * Matches BSP plat/hisilicon/hi3798cv200/resume_entry_32bit/start.S sequence.
 * This bridge never copies Image/DTB, touches GIC, or enters cold _start.
 */
.syntax unified
.arch armv7-a
.arm
.section .text.start,"ax",%progbits
.global _start
_start:
	cpsid aif
	ldr r3, =0xf8000000
	ldr r2, [r3, #0xf8]
	ldr r3, =0xf8a80000
	str r2, [r3, #0x34]
	ldr r2, [r3, #0x30]
	orr r2, r2, #0xf
	str r2, [r3, #0x30]
	ldr r3, =0xf8bb0000
	mov r2, #1
	str r2, [r3, #0x20]
	movw r2, #0x103
	str r2, [r3]
	dsb sy
	isb
	mrc p15, 0, r3, c12, c0, 2
	orr r3, r3, #3
	mcr p15, 0, r3, c12, c0, 2
	isb
1:
	wfi
	b 1b
