/* SPDX-License-Identifier: GPL-2.0-only */
/*
 * Shared HiSTB cold-boot AArch32 to AArch64 trampoline.
 * HISTB_HI3798MV310 selects the EC6110T/MV310 handoff banner.
 *
 * U-Boot loads this complete payload at 0x12000000.  Before requesting the
 * AArch64 warm reset, copy the embedded Linux Image, DTB and EL handoff to
 * their final addresses.
 */

	.syntax unified
	.arch armv7-a
	.arm
	.section .text.start, "ax", %progbits
	.global _start
	.type _start, %function

_start:
	cpsid	if

#ifdef HISTB_HI3798MV310
	ldr	r0, =message_mv310
	bl	puts
#endif

	ldr	r0, =message_copy
	bl	puts

	ldr	r0, =arm64_image
	ldr	r1, =0x02000000
	ldr	r2, =arm64_image_end - arm64_image
	bl	copy_blob

	ldr	r0, =arm64_dtb
	ldr	r1, =0x10000000
	ldr	r2, =arm64_dtb_end - arm64_dtb
	bl	copy_blob

	ldr	r0, =arm64_entry
	ldr	r1, =0x11000000
	ldr	r2, =arm64_entry_end - arm64_entry
	bl	copy_blob

	/* Publish the copied payload even when Fastboot left D-cache enabled. */
	ldr	r0, =0x02000000
	ldr	r1, =arm64_image_end - arm64_image
	bl	clean_blob_to_poc
	ldr	r0, =0x10000000
	ldr	r1, =arm64_dtb_end - arm64_dtb
	bl	clean_blob_to_poc
	ldr	r0, =0x11000000
	ldr	r1, =arm64_entry_end - arm64_entry
	bl	clean_blob_to_poc
	mov	r0, #0
	mcr	p15, 0, r0, c7, c5, 0
	mcr	p15, 0, r0, c7, c5, 6
	dsb	sy
	isb
	ldr	r0, =message_reset
	bl	puts

	/* Match the system-counter setup in the HiSilicon ARM64 boot path. */
	ldr	r0, =0xf8bb0020
	mov	r1, #1
	str	r1, [r0]
	ldr	r0, =0xf8bb0000
	ldr	r1, =0x103
	str	r1, [r0]

	/* Core 0 resumes at the AArch64 EL handoff stub after warm reset. */
	ldr	r0, =0xf8a80034
	ldr	r1, =0x11000000
	str	r1, [r0]

	ldr	r0, =0xf8a80030
	ldr	r1, [r0]
	orr	r1, r1, #1
	str	r1, [r0]

	dsb	sy
	isb
	mrc	p15, 0, r0, c12, c0, 2
	orr	r0, r0, #3
	mcr	p15, 0, r0, c12, c0, 2
	isb

1:
	wfi
	b	1b

copy_blob:
	cmp	r2, #4
	blo	3f
2:
	ldr	r3, [r0], #4
	str	r3, [r1], #4
	subs	r2, r2, #4
	cmp	r2, #4
	bhs	2b
3:
	cmp	r2, #0
	bxeq	lr
4:
	ldrb	r3, [r0], #1
	strb	r3, [r1], #1
	subs	r2, r2, #1
	bne	4b
	bx	lr

/* r0=start, r1=size. CTR.DminLine defines the D-cache line in words. */
clean_blob_to_poc:
	add	r1, r0, r1
	mrc	p15, 0, r2, c0, c0, 1
	ubfx	r2, r2, #16, #4
	mov	r3, #4
	lsl	r3, r3, r2
	sub	r2, r3, #1
	bic	r0, r0, r2
	add	r1, r1, r2
	bic	r1, r1, r2
clean_blob_line:
	cmp	r0, r1
	bhs	clean_blob_done
	mcr	p15, 0, r0, c7, c10, 1
	add	r0, r0, r3
	b	clean_blob_line
clean_blob_done:
	dsb	sy
	bx	lr

puts:
	ldrb	r1, [r0], #1
	cmp	r1, #0
	bxeq	lr
5:
	ldr	r2, =0xf8b00018
	ldr	r3, [r2]
	tst	r3, #0x20
	bne	5b
	ldr	r2, =0xf8b00000
	strb	r1, [r2]
	b	puts

#ifdef HISTB_HI3798MV310
message_mv310:
	.asciz	"\r\n[EC6110T/MV310] A32 to A64 RAM handoff\r\n"
#endif

message_copy:
	.asciz	"\r\n[A32] unpacking bundled ARM64 payload\r\n"
message_reset:
	.asciz	"[A32] requesting AArch64 warm reset\r\n"

	.ltorg
	.size _start, . - _start

	.balign 64
arm64_entry:
	.incbin ARM64_ENTRY_BIN
arm64_entry_end:

	.balign 64
arm64_dtb:
	.incbin ARM64_DTB_BIN
arm64_dtb_end:

	.balign 64
arm64_image:
	.incbin ARM64_IMAGE_BIN
arm64_image_end:
