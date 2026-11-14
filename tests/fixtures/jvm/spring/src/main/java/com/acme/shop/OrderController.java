package com.acme.shop;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/orders")
public class OrderController {
    private final PriceService prices;

    public OrderController(PriceService prices) {
        this.prices = prices;
    }

    @GetMapping("/{sku}")
    public String quote(String sku) {
        return label(prices.price(sku));
    }

    private String label(long cents) {
        return String.valueOf(cents);
    }
}
