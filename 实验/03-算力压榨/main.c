#include <reg52.h>
#include "uart_put.h"

#define LED P2

/* 算力压榨：同一条链 x = x*3141+12345 (mod 65536)，op 定义与实验02 一致。
   A/B/C 三种实现轮流各跑一个 8 溢出窗口（524288 拍 ≈ 568.9ms）：
   A = 实验02 原始循环（?C?IMUL 库乘 26 拍 + 32 位计数）
   B = 16 位乘拆 3 次 8 位 MUL、16 位计数、TF0 每 4 步一查（实测 16242/s）
   C = 再压榨：u 高 8 位本来就被丢弃 → 后两次 MUL 只取低位字节；
       x 用 x_lo/x_hi 双 uchar 状态，免每次拆装 16 位；ops 按块 +4
   计时窗口与换算同实验02：per_sec = ops*17578/10000。 */

#define STEP() do { \
    unsigned char lo = (unsigned char)x; \
    unsigned char hi = (unsigned char)(x >> 8); \
    unsigned int b = (unsigned int)(lo * (unsigned char)0x45); \
    unsigned int u = (unsigned int)(hi * (unsigned char)0x45) + (b >> 8) \
                   + (unsigned int)(lo * (unsigned char)0x0C); \
    x = (((u & 0xFF) << 8) | (b & 0xFF)) + 12345U; \
} while (0)

#define STEP2() do { \
    unsigned int bb = (unsigned int)(x_lo * (unsigned char)0x45); \
    unsigned char s = (unsigned char)(x_hi * (unsigned char)0x45) \
                    + (unsigned char)(x_lo * (unsigned char)0x0C); \
    unsigned int tt = bb + 0x39U; \
    x_lo = (unsigned char)tt; \
    x_hi = s + (unsigned char)(tt >> 8) + 0x30U; \
} while (0)

static void report(const char *tag, unsigned long ops, unsigned int x)
{
    uart_send_str("s=");
    uart_send_str(tag);
    uart_send_str(" ops=");
    uart_put_num((long)ops);
    uart_send_str(" per_sec=");
    uart_put_num((long)((ops * 17578UL) / 10000UL));
    uart_send_str(" x=");
    uart_put_num((long)x);
    uart_send_str("\r\n");
}

void main(void)
{
    unsigned char hb = 0;
    unsigned char ovf;
    unsigned long ops;
    unsigned long per_a;
    unsigned int ovf_b;
    unsigned int ops_b;
    unsigned long per_b;
    unsigned char ovf_c;
    unsigned int ops_c;
    unsigned long per_c;
    unsigned char x_lo;
    unsigned char x_hi;
    unsigned int x;

    uart_init();

    TMOD &= 0xF0;       /* 清 Timer0 位，保留 Timer1（uart_init 的波特率发生器） */
    TMOD |= 0x01;       /* Timer0 模式 1：16 位 */
    TH0 = 0;
    TL0 = 0;
    TF0 = 0;
    TR0 = 1;

    x = 1;
    uart_send_str("bench03 A/B/C\r\n");

    while (1)
    {
        /* ---- 窗口 A：实验02 基线 ---- */
        TF0 = 0;
        while (!TF0);        /* 等下一个溢出沿：窗口同相位起跑 */
        TF0 = 0;
        ops = 0;
        ovf = 0;
        while (ovf < 8)
        {
            x = x * 3141 + 12345;
            ops = ops + 1;
            if (TF0) { TF0 = 0; ovf = ovf + 1; }
        }
        per_a = (ops * 17578UL) / 10000UL;
        report("A", ops, x);

        /* ---- 窗口 B：压榨 ---- */
        TF0 = 0;
        while (!TF0);
        TF0 = 0;
        ops_b = 0;
        ovf_b = 0;
        while (ovf_b < 8)
        {
            STEP(); ops_b = ops_b + 1;
            STEP(); ops_b = ops_b + 1;
            STEP(); ops_b = ops_b + 1;
            STEP(); ops_b = ops_b + 1;
            if (TF0) { TF0 = 0; ovf_b = ovf_b + 1; }
        }
        per_b = ((unsigned long)ops_b * 17578UL) / 10000UL;
        report("B", (unsigned long)ops_b, x);

        /* ---- 窗口 C：再压榨 ---- */
        TF0 = 0;
        while (!TF0);
        TF0 = 0;
        ops_c = 0;
        ovf_c = 0;
        x_lo = (unsigned char)x;
        x_hi = (unsigned char)(x >> 8);
        while (ovf_c < 8)
        {
            STEP2(); STEP2(); STEP2(); STEP2();
            ops_c = ops_c + 4;
            if (TF0) { TF0 = 0; ovf_c = ovf_c + 1; }
        }
        x = ((unsigned int)x_hi << 8) | x_lo;
        per_c = ((unsigned long)ops_c * 17578UL) / 10000UL;
        report("C", (unsigned long)ops_c, x);

        uart_send_str("v: ");
        uart_put_num((long)per_a);
        uart_send_str(",");
        uart_put_num((long)per_b);
        uart_send_str(",");
        uart_put_num((long)per_c);
        uart_send_str("\r\n");

        if (hb) { LED = ~0x01; hb = 0; }
        else    { LED = 0xFF;  hb = 1; }
    }
}
