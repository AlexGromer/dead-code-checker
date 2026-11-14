package com.acme.shop;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

@Configuration
public class ShopConfig {
    @Bean
    public Auditor auditor(PriceService audited) {
        return new Auditor(audited);
    }
}
