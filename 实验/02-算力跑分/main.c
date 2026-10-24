#include <reg52.h>
#include "uart_put.h"

#define LED P2

/* 算力跑分：Timer0 计时窗口内完成多少次 16 位乘加。
   机器周期 = 12/11.0592MHz = 1.085us；Timer0 16 位溢出周期 65536 拍 ≈ 71.1ms；
   8 次溢出 = 524288 拍 ≈ 568.9ms 固定窗口。每秒次数 = ops * 921600 / 524288。 */

void main(void)
{
    unsigned char ovf;
    unsigned char hb = 0;
    unsigned long ops;
    unsigned long per_sec;
    unsigned int x;

    uart_init();

    TMOD &= 0xF0;       /* 清 Timer0 位，保留 Timer1（uart_init 的波特率发生器） */
    TMOD |= 0x01;       /* Timer0 模式 1：16 位 */
    TH0 = 0;
    TL0 = 0;
    TF0 = 0;
    TR0 = 1;

    x = 1;
    while (1)
    {
        TF0 = 0;
        while (!TF0);        /* 等下一个溢出沿：每个窗口都从同相位起跑，ops 才稳 */
        TF0 = 0;
        ovf = 0;
        ops = 0;
        while (ovf < 8)
        {
            x = x * 3141 + 12345;
            ops = ops + 1;
            if (TF0) { TF0 = 0; ovf = ovf + 1; }
        }
        per_sec = (ops * 17578UL) / 10000UL;

        uart_send_str("ops=");
        uart_put_num((long)ops);
        uart_send_str(" per_sec=");
        uart_put_num((long)per_sec);
        uart_send_str(" x=");
        uart_put_num((long)x);
        uart_send_str("\r\n");
        uart_curve((long)per_sec);

        if (hb) { LED = ~0x01; hb = 0; }
        else    { LED = 0xFF;  hb = 1; }
    }
}
