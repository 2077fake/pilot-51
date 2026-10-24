#include <reg52.h>
#include "uart_put.h"

/* 数码管计数动画：8 位数码管动态扫描，显示一个每 ~0.1s +1 的计数器，
   同步发调试台（终端 t= 报数 + v: 曲线楼梯线）。
   硬件映射 [推断：普中 A2 / HC6800-ES 通用接法，待真机确认，翻车则单行修复]：
   - 段码 = P0（经 74HC245，共阴编码，1=亮）
   - 位选 = 74HC138，A/B/C = P2.2/P2.3/P2.4，Y_i 低有效选第 i 位
   - P2 同时挂 LED 模块：位 2-4 随位选闪动属正常（共用脚），其余位保持熄灭 */

#define SEG_BLANK 0x00

static unsigned char code SEG10[10] = {
    0x3F, 0x06, 0x5B, 0x4F, 0x66, 0x6D, 0x7D, 0x07, 0x7F, 0x6F
};

static unsigned char seg[8];   /* 当前帧 8 位段码，0x00 = 消隐 */

static void delay_1ms(void)    /* ~1ms @11.0592MHz，与模板 delay 同款标定 */
{
    unsigned int j;
    for (j = 0; j < 114; j++);
}

static void scan_frame(void)   /* 8 位扫一遍 ≈ 8.5ms（>100Hz 无闪烁） */
{
    unsigned char i;
    for (i = 0; i < 8; i++)
    {
        P0 = SEG_BLANK;                       /* 切位选前消隐防鬼影 */
        P2 = (P2 & 0xE3) | (i << 2);
        P0 = seg[i];
        delay_1ms();
    }
    P0 = SEG_BLANK;
}

static void to_seg(unsigned long v)
{
    unsigned char dig[8];
    unsigned char i, first = 7;

    v = v % 100000000UL;
    for (i = 0; i < 8; i++)                    /* dig[7] = 个位 */
    {
        dig[7 - i] = (unsigned char)(v % 10UL);
        v /= 10UL;
    }
    for (i = 0; i < 7; i++)                    /* 前导零消隐，末位必显示 */
    {
        if (dig[i] != 0) { first = i; break; }
    }
    for (i = 0; i < 8; i++)
        seg[i] = (i < first) ? SEG_BLANK : SEG10[dig[i]];
}

void main(void)
{
    unsigned long counter = 0;
    unsigned char frames = 0;

    uart_init();
    P2 = 0xFF;                 /* LED 全灭，位选脚置高 */
    P0 = SEG_BLANK;
    to_seg(counter);
    uart_send_str("tube04 counter\r\n");

    while (1)
    {
        scan_frame();
        frames++;
        if (frames >= 12)      /* 12 帧 ≈ 0.1s */
        {
            frames = 0;
            counter = counter + 1;
            to_seg(counter);
            uart_send_str("t=");
            uart_put_num((long)counter);
            uart_send_str("\r\n");
            uart_curve((long)counter);
        }
    }
}
