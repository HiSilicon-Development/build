/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef HI3798CV200_PM_H
#define HI3798CV200_PM_H
/* CV200 resident monitor: no heap, MMU, or Linux address-space dependency. */
typedef unsigned int u32;
typedef unsigned long u64;
struct pm_frame { u64 x[31]; u64 pad; };
extern unsigned char __pm_bss_start[], __pm_bss_end[];
extern unsigned char pm_warm_entry[], pm_a32_bridge[];
extern void pm_clean_all(void), pm_clean_private(void);
extern void pm_launch(u64 entry, u64 context) __attribute__((noreturn));
extern void pm_cpu_on_launch(u64 entry, u64 context) __attribute__((noreturn));
extern void pm_deep_wait(void); /* returns only on rejected MCU start */
void pm_init(void);
void pm_dispatch(struct pm_frame *f);
void pm_secondary(void) __attribute__((noreturn));
void pm_warm(void) __attribute__((noreturn));
#endif
