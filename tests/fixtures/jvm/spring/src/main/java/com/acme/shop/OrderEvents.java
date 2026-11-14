package com.acme.shop;

import org.springframework.kafka.annotation.KafkaListener;
import org.springframework.stereotype.Service;

@Service
public class OrderEvents {
    @KafkaListener(topics = "orders")
    public void onOrder(String payload) {
        handle(payload);
    }

    private void handle(String payload) {
        payload.trim();
    }
}
