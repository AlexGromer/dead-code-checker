package com.acme.shop;

import org.junit.jupiter.api.Test;

public class PriceServiceTest {
    @Test
    void standardAddsOne() {
        StandardPriceService svc = new StandardPriceService();
        svc.price("a");
    }
}
