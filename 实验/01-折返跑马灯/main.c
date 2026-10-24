#include <reg52.h>
#include "uart_put.h"

#define LED P2

void delay_ms(unsigned int ms)
{
    unsigned int i, j;
    for (i = 0; i < ms; i++)
        for (j = 0; j < 114; j++);
}

void main(void)
{
    char k = 0;
    char dir = 1;
    unsigned int n = 0;

    uart_init();
    while (1)
    {
        LED = ~(0x01 << k);
        n = n + 1;
        uart_curve2(k, n);
        delay_ms(200);
        k = k + dir;
        if (k == 7) dir = -1;
        if (k == 0) dir = 1;
    }
}
