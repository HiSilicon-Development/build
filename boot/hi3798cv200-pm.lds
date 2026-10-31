/* SPDX-License-Identifier: GPL-2.0-only */
OUTPUT_ARCH(aarch64)
ENTRY(_start)
SECTIONS
{
  . = 0x11000000;
  .text : { KEEP(*(.text.start)) *(.text.pm) *(.text .text.*) }
  .rodata : { *(.rodata .rodata.*) }
  .data : { *(.data .data.*) }
  . = ALIGN(16);
  __pm_bss_start = .;
  .bss (NOLOAD) : { *(.bss .bss.* COMMON) }
  __pm_bss_end = .;
  ASSERT(. <= 0x1100d000, "CV200 resident monitor exceeds code/data budget")
  . = 0x1100d000;
  .pm_stacks (NOLOAD) : { . += 0x2000; }
  __pm_stack_top = .;
  ASSERT(. == 0x1100f000, "CV200 monitor overlaps release mailbox")
  /DISCARD/ : { *(.comment) *(.note*) *(.eh_frame*) }
}
