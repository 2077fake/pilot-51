#include <reg52.h>
#include "uart_put.h"

/* 11.0592MHz / 9600-8N1：T1 方式 2 自动重装
 * 256 - 11059200 / 12 / 32 / 9600 = 253 = 0xFD
 * 换晶振或换波特率只改这一个数。 */
#define UART_RELOAD 0xFD

void uart_init(void)
{
    TMOD = (unsigned char)((TMOD & 0x0F) | 0x20);
    TH1 = UART_RELOAD;
    TL1 = UART_RELOAD;
    SCON = 0x50;
    TR1 = 1;
}

void uart_send_byte(unsigned char b)
{
    SBUF = b;
    while (TI == 0)
        ;
    TI = 0;
}

void uart_send_str(const char *s)
{
    while (*s)
        uart_send_byte((unsigned char)*s++);
}

void uart_put_num(long v)
{
    unsigned char digit[12];
    unsigned char n = 0;
    unsigned char i;

    if (v < 0)
    {
        uart_send_byte('-');
        v = -v;
    }
    do
    {
        digit[n++] = (unsigned char)(v % 10);
        v = v / 10;
    } while (v != 0 && n < 12);

    for (i = n; i > 0; i--)
        uart_send_byte((unsigned char)('0' + digit[i - 1]));
}

void uart_curve(long v)
{
    uart_send_str("v: ");
    uart_put_num(v);
    uart_send_str("\r\n");
}

void uart_curve2(long a, long b)
{
    uart_send_str("v: ");
    uart_put_num(a);
    uart_send_str(",");
    uart_put_num(b);
    uart_send_str("\r\n");
}
