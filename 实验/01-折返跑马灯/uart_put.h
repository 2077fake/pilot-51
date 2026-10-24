#ifndef UART_PUT_H
#define UART_PUT_H

/* STC89C52 @ 11.0592MHz，9600-8N1， polled 发送，不占中断。 */

void uart_init(void);
void uart_send_byte(unsigned char b);
void uart_send_str(const char *s);
void uart_put_num(long v);

/* 调试台的曲线协议：一行一个或多个数值，逗号分隔。 */
void uart_curve(long v);
void uart_curve2(long a, long b);

#endif
